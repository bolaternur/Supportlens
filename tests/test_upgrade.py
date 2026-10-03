from datetime import datetime, timedelta
import json
from io import BytesIO
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from supportlens.data import ARTICLES
from supportlens.engine import (AIError, Settings, analyze, extract_known, probe_connection, validate_reply,
                                numbers, call_api, PROBE_SCHEMA)
from supportlens.storage import ConflictError, Store, LOCAL_TZ


def classification(**changes):
    return dict(topic="доставка", priority="обычный", topic_reason="Доставка.", priority_reason="Справочный вопрос.", language="ru", entities=[], article_ids=["KB-004"],question_parts=[{"kind":"company","text":"","reason":"Правила магазина."}]) | changes


def plan(**changes):
    return dict(sources=["KB-004:p2"], segments=[{"text":"Доставка стоит 1500 ₸, от 20000 ₸ — бесплатно.","source_ids":["KB-004"]}], missing_fields=[], operator_notes="Общий вопрос.", action="Проверить данные") | changes


def rich_plan():
    p = plan()
    p["segments"][0].update(kind="kb", explanation="По статье о доставке.")
    return p


def operator_plan():
    p = rich_plan()
    return dict(sources=p["sources"], segments=p["segments"], wants_draft=True, question_type="ticket")


class RegressionTests(unittest.TestCase):
    def process(self, message, **kw):
        return analyze(message, ARTICLES, Settings(), **kw)

    def test_negations_are_not_incidents(self):
        for text in ["Не было двойного списания, только хочу узнать, как оплатить", "Двойного списания нет. Как оплатить картой?", "Деньги не списали два раза. Какие способы оплаты есть?"]:
            with self.subTest(text=text):
                r = self.process(text)
                self.assertEqual(r["priority"], "обычный")
                self.assertNotIn("KB-003", [s["id"] for s in r["sources"]])

    def test_hypothetical_takeover_and_payment(self):
        for text in ["Боюсь, что аккаунт могут взломать. Как сменить пароль?", "Если деньги спишут дважды, что делать?", "Как предотвратить взлом аккаунта?"]:
            self.assertEqual(self.process(text)["priority"], "обычный")
        self.assertEqual(self.process("Похоже, аккаунт взломали: кто-то сменил пароль.")["priority"], "критический")
        self.assertEqual(self.process("Кажется, деньги списали дважды.")["priority"], "высокий")

    def test_amount_is_not_an_order_number(self):
        r = self.process("Списали дважды по 18000 тенге, номера заказа у меня нет")
        self.assertEqual(r["known_fields"]["amount"], "18000")
        self.assertEqual(r["known_fields"]["order"], "")
        self.assertIn("order", r["missing_fields"])
        self.assertNotIn("amount", r["missing_fields"])

    def test_extraction_is_contextual(self):
        known = extract_known("Заказ № 1042, сумма 18 000 тенге. Алматы. Оплата 03.10.2026. Последние 4 цифры 5678.")
        self.assertEqual(known["order"], "1042")
        self.assertEqual(known["amount"], "18 000")
        self.assertEqual(known["city"], "Алматы")
        self.assertEqual(known["dates"], "03.10.2026")
        self.assertEqual(known["card_last4"], "5678")
        self.assertEqual(extract_known("Товар стоит 18000 тенге")["order"], "")

    def test_partial_known_question_keeps_evidence(self):
        r = self.process("Можно оплатить бонусами и банковской картой?")
        self.assertIn("KB-001", [s["id"] for s in r["sources"]])
        self.assertIn("Visa", r["draft"])
        self.assertIn("нет подтверждённых правил", r["draft"])

    def test_general_delivery_does_not_ask_for_order_or_city(self):
        r = self.process("Сколько стоит доставка в Алматы?")
        self.assertEqual(r["known_fields"]["city"], "Алматы")
        self.assertEqual(r["missing_fields"], [])
        self.assertIn("1500", r["draft"])
        self.assertNotIn("номер заказа", r["draft"])
        self.assertNotIn("демонстрацион", r["draft"])

    def test_kazakh_entire_reply_and_mixed_input(self):
        r = self.process("Алматыға жеткізу қанша тұрады?")
        self.assertEqual(r["language"], "kk")
        self.assertNotIn("Для проверки", r["draft"])
        self.assertIn("Жеткізу", r["draft"])
        self.assertEqual(r["missing_fields"], [])
        self.assertEqual(self.process("Ақша екі рет алынды, номера заказа нет")["priority"], "высокий")

    def test_numbers_normalize_thousands_without_inventing_values(self):
        self.assertEqual(numbers("1 500 ₸, 20,000 ₸"), {"1500","20000"})

    def test_threshold_boundary_is_checked_without_model_verdict(self):
        source = {"id":"KB-004","quote":ARTICLES[3]["body"].split("\n\n")[2]}
        for text,language in [("Бесплатная доставка при сумме больше 20000 ₸.","ru"),("20000 ₸-тан артық болса, жеткізу тегін.","kk")]:
            p = plan(sources=[source],segments=[{"text":text,"source_ids":["KB-004"]}])
            with self.assertRaises(AIError):
                validate_reply(p,"Сколько стоит доставка в Алматы?",[ARTICLES[3]],language)
        p = plan(sources=[source],segments=[{"text":"20000 ₸-ден бастап жеткізу тегін.","source_ids":["KB-004"]}])
        self.assertIn("тегін",validate_reply(p,"Сколько стоит доставка в Алматы?",[ARTICLES[3]],"kk")["draft"])

    def test_fact_validation_rejects_amount_action_and_irrelevant_source(self):
        source = {"id":"KB-004", "quote":ARTICLES[3]["body"].split("\n\n")[2]}
        for text in ["Доставка стоит 999 ₸.", "Мы уже отправили ваш заказ.", "Обращение требует высокого приоритета."]:
            p = plan(sources=[source], segments=[{"text":text,"source_ids":["KB-004"]}])
            with self.assertRaises(AIError):
                validate_reply(p,"Сколько стоит доставка в Алматы?",[ARTICLES[3]],"ru")
        with self.assertRaises(AIError):
            validate_reply(plan(sources=[source]),"Можно оплатить криптовалютой?",[ARTICLES[3]],"ru")

    def test_review_rejects_semantically_wrong_condition_with_same_numbers(self):
        p = plan(segments=[{"text":"Бесплатная доставка от 1500 ₸.","source_ids":["KB-004"]}])
        transport = Mock(side_effect=[classification(),p,{"supported":False,"reason":"Порог не совпадает."}])
        result = analyze("Сколько стоит доставка?",ARTICLES,Settings("test-key"),transport)
        self.assertEqual(result["mode"],"fallback")
        self.assertEqual(result["error_category"],"grounding")

    def test_known_fields_removed_from_model_questions(self):
        transport = Mock(side_effect=[classification(),plan(missing_fields=["city","order"]),{"supported":True,"reason":"Совпадает."}])
        result = analyze("Сколько стоит доставка в Алматы?",ARTICLES,Settings("test-key"),transport)
        self.assertEqual(result["mode"],"ai")
        self.assertEqual(result["missing_fields"],[])

    def test_model_question_is_not_duplicated_and_card_warning_remains(self):
        article = ARTICLES[2]
        p = plan(sources=[{"id":article["id"],"quote":article["body"].split("\n\n")[1]}],
                 segments=[{"text":"Пожалуйста, пришлите последние 4 цифры карты для сверки.","source_ids":[article["id"]]}])
        r = validate_reply(p,"За заказ 5170 деньги списали два раза по 18000 тенге сегодня.",[article],"ru")
        self.assertEqual(r["draft"].count("4 цифры"),1)
        self.assertNotIn("Укажите только последние",r["draft"])
        self.assertIn("CVV",r["draft"])

    def test_model_cannot_ask_known_city_in_client_segment(self):
        p = plan(sources=[{"id":"KB-004","quote":ARTICLES[3]["body"].split("\n\n")[2]}],segments=[{"text":"Укажите город доставки.","source_ids":["KB-004"]}])
        with self.assertRaises(AIError):
            validate_reply(p,"Сколько стоит доставка в Алматы?",[ARTICLES[3]],"ru")


class StorageUpgradeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/"db.sqlite3"
        self.s = Store(self.path)
        self.s.initialize(seed=False)

    def tearDown(self):
        self.temp.cleanup()

    def ticket(self, message="Сколько стоит доставка в Алматы?"):
        id = self.s.create_ticket(message)
        self.s.process(id,Settings())
        return id

    def test_analysis_race_does_not_overwrite_approval(self):
        id = self.ticket()
        t = self.s.ticket(id)
        calls = 0
        def race(settings,schema,payload):
            nonlocal calls
            calls += 1
            if calls == 1:
                self.s.save_draft(id,"Проверенный ответ другого сеанса",approve=True,expected_revision=t["revision"])
                return classification()
            return plan() if calls == 2 else {"supported":True,"reason":"Совпадает."}
        with self.assertRaises(ConflictError):
            self.s.process(id,Settings("test-key"),race,expected_revision=t["revision"])
        actual = self.s.ticket(id)
        self.assertEqual(actual["status"],"утверждено")
        self.assertEqual(actual["approved_answer"],"Проверенный ответ другого сеанса")
        self.assertTrue(any(r["event"] == "analysis_conflict" for r in self.s.audit(id)))

    def test_stale_edit_and_classification_are_rejected(self):
        id = self.ticket()
        version = self.s.ticket(id)["revision"]
        self.s.save_draft(id,"Первый оператор",expected_revision=version)
        with self.assertRaises(ConflictError):
            self.s.save_draft(id,"Второй оператор",expected_revision=version)
        with self.assertRaises(ConflictError):
            self.s.correct(id,"другое","низкий",expected_revision=version)
        self.assertEqual(self.s.ticket(id)["draft"],"Первый оператор")

    def test_idempotent_intake_and_processing_not_repeated_by_reads(self):
        first = self.s.create_ticket("Сообщение", "same-form")
        second = self.s.create_ticket("Сообщение", "same-form")
        self.assertEqual(first,second)
        self.assertEqual(len(self.s.tickets()),1)
        self.s.process(first,Settings())
        for _ in range(3):
            self.s.initialize(seed=False)
            self.s.ticket(first)
            self.s.metrics()
        self.assertEqual(self.s.metrics()["processing_runs"],1)

    def test_article_update_keeps_approved_answer_snapshots(self):
        id = self.ticket()
        t = self.s.ticket(id)
        self.s.save_draft(id,t["draft"],approve=True)
        old_snapshot = self.s.ticket(id)["source_snapshots"]
        a = next(a for a in self.s.articles() if a["id"] == "KB-004")
        self.s.save_article(a["id"],a["title"],a["topic"],a["body"].replace("1500","1700"),a["keywords"],a["fields"],a["action"],a["version"])
        self.s.initialize(seed=False)
        self.assertEqual(self.s.ticket(id)["source_snapshots"],old_snapshot)
        self.assertIn("1500",self.s.ticket(id)["approved_answer"])
        self.s.save_draft(id,self.s.ticket(id)["draft"],approve=True)
        self.assertIn("1700",next(a for a in self.s.articles() if a["id"] == "KB-004")["body"])

    def test_custom_article_and_conflict(self):
        self.s.save_article("KB-016","Самовывоз","доставка","Получение товара в пункте выдачи согласуется со специалистом.",["самовывоз"],[],"Передать специалисту")
        self.s.initialize(seed=False)
        self.assertEqual(len(self.s.articles()),16)
        a = self.s.articles()[-1]
        with self.assertRaises(ConflictError):
            self.s.save_article(a["id"],a["title"],a["topic"],a["body"],a["keywords"],[],a["action"],0)

    def test_period_and_sorting(self):
        self.s.initialize()
        today = datetime.now(LOCAL_TZ).date()
        m = self.s.metrics(start=today,end=today)
        self.assertEqual(sum(m["topics"].values()),m["total"])
        self.assertLess(m["total"],50)
        queue = self.s.tickets()
        self.assertNotEqual(queue[0]["status"],"утверждено")
        self.assertEqual(queue[0]["priority"],"критический")

    def test_ai_status_not_configured_unverified_ready_error(self):
        disabled = Settings()
        self.assertEqual(self.s.ai_state(disabled)["state"],"not_configured")
        settings = Settings("test-key")
        self.assertEqual(self.s.ai_state(settings)["state"],"unverified")
        self.s.check_ai(settings,lambda *a:{"status":"ok"})
        self.assertEqual(self.s.ai_state(settings)["state"],"ready")
        def failure(*a):
            raise AIError("Ошибка авторизации.","auth")
        self.s.check_ai(settings,failure)
        self.assertEqual(self.s.ai_state(settings)["category"],"auth")
        self.assertNotIn("test-key",self.path.read_bytes().decode("latin1"))

    def test_probe_without_key_never_calls_network(self):
        transport = Mock()
        self.assertEqual(probe_connection(Settings(),transport)["state"],"not_configured")
        transport.assert_not_called()

    def test_conversations_are_isolated_and_apply_is_explicit(self):
        a,b = self.ticket(),self.ticket("Как работает доставка в Астане?")
        before = self.s.ticket(a)
        response = operator_plan()
        proposal = rich_plan()
        transport = Mock(side_effect=[response,{"supported":True,"reason":"Обоснован."},proposal,{"supported":True,"reason":"Обоснован."}])
        self.s.ask_assistant(a,"Сделай ответ короче",Settings("test-key"),before["revision"],transport)
        self.assertEqual(self.s.ticket(a)["draft"],before["draft"])
        self.assertEqual(len(self.s.conversation(a)),2)
        self.assertEqual(self.s.conversation(b),[])
        self.assertEqual(transport.call_args_list[0].args[2]["history"],[])
        message = self.s.conversation(a)[-1]
        with self.assertRaises(ConflictError):
            self.s.apply_suggestion(b,message["id"],self.s.ticket(b)["revision"])
        self.s.apply_suggestion(a,message["id"],before["revision"])
        self.assertEqual(self.s.ticket(a)["draft"],proposal["segments"][0]["text"])

    def test_stale_chat_proposal_cannot_replace_approved_answer(self):
        id = self.ticket()
        before = self.s.ticket(id)
        response = operator_plan()
        self.s.ask_assistant(id,"Сократи",Settings("test-key"),before["revision"],Mock(side_effect=[response,{"supported":True,"reason":"Обоснован."},rich_plan(),{"supported":True,"reason":"Обоснован."}]))
        proposal = self.s.conversation(id)[-1]
        self.s.save_draft(id,"Утверждённый ответ",approve=True,expected_revision=before["revision"])
        with self.assertRaises(ConflictError):
            self.s.apply_suggestion(id,proposal["id"],self.s.ticket(id)["revision"])
        self.assertEqual(self.s.ticket(id)["approved_answer"],"Утверждённый ответ")

    def test_legacy_migration_keeps_ticket_approval_and_audit(self):
        id = self.ticket()
        self.s.save_draft(id,"Старый утверждённый ответ",approve=True)
        with self.s.connection() as con:
            expected = dict(con.execute("SELECT * FROM tickets WHERE id=?",(id,)).fetchone())
            events = [dict(r) for r in con.execute("SELECT * FROM audit")]
            con.execute("DELETE FROM metadata WHERE key='migration_v2'")
            con.execute("DROP TABLE answer_versions")
            con.execute("DROP INDEX intake_once")
            for column in ["revision","draft_revision","language","known_fields","operator_notes","error_category","source_snapshots","intake_token"]:
                con.execute(f"ALTER TABLE tickets DROP COLUMN {column}")
            con.execute("ALTER TABLE articles DROP COLUMN version")
        self.s.initialize(seed=False)
        migrated = self.s.ticket(id)
        for field in ["message","draft","approved_answer","approved_at","status","sources"]:
            self.assertEqual(migrated[field],json.loads(expected[field]) if field == "sources" else expected[field])
        with self.s.connection() as con:
            self.assertEqual([dict(r) for r in con.execute("SELECT * FROM audit")],events)
        self.assertEqual(len(self.s.versions(id)),1)
        self.s.initialize(seed=False)
        self.assertEqual(len(self.s.versions(id)),1)


class ConfigurationTests(unittest.TestCase):
    def test_provider_json_rejection_retries_once_with_validated_json_mode(self):
        failed = HTTPError("https://api.groq.com",400,"Bad request",{},BytesIO(b'{"error":{"code":"json_validate_failed","message":"test-key"}}'))
        envelope = {"choices":[{"finish_reason":"stop","message":{"content":"{\"status\":\"ok\"}"}}]}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(envelope).encode()
        with patch("supportlens.engine.request.build_opener") as opener:
            opener.return_value.open.side_effect = [failed,response]
            self.assertEqual(call_api(Settings("test-key"),PROBE_SCHEMA,{"task":"probe"}),{"status":"ok"})
            bodies = [json.loads(c.args[0].data) for c in opener.return_value.open.call_args_list]
            self.assertEqual([b["response_format"]["type"] for b in bodies],["json_schema","json_object"])
            self.assertNotIn("test-key",json.dumps(bodies))

    def test_rate_limit_is_not_retried_and_provider_body_is_private(self):
        failed = HTTPError("https://api.groq.com",429,"Limit",{},BytesIO(b'{"error":{"message":"test-key"}}'))
        with patch("supportlens.engine.request.build_opener") as opener:
            opener.return_value.open.side_effect = failed
            with self.assertRaises(AIError) as caught:
                call_api(Settings("test-key"),PROBE_SCHEMA,{"task":"probe"})
            self.assertEqual(caught.exception.category,"limit")
            self.assertNotIn("test-key",str(caught.exception))
            self.assertEqual(opener.return_value.open.call_count,1)

    def test_env_reload_and_environment_precedence(self):
        with patch("supportlens.engine.dotenv_values",return_value={"AI_API_KEY":"local-secret","AI_BASE_URL":"https://api.groq.com/openai/v1","AI_MODEL":"openai/gpt-oss-20b"}),patch.dict(os.environ,{"AI_API_KEY":"environment-secret"}):
            s = Settings.from_env()
            self.assertEqual(s.api_key,"environment-secret")
            self.assertEqual(s.provider,"Groq")
            self.assertNotIn(s.api_key,s.fingerprint)

    def test_json_object_mode_is_validated_and_secrets_not_echoed(self):
        envelope = {"choices":[{"finish_reason":"stop","message":{"content":"{\"status\":\"ok\"}"}}]}
        with patch("supportlens.engine.request.build_opener") as opener:
            opener.return_value.open.return_value.__enter__.return_value.read.return_value = json.dumps(envelope).encode()
            s = Settings("test-key",response_format="json_object")
            call_api(s,PROBE_SCHEMA,{"task":"probe"})
            body = json.loads(opener.return_value.open.call_args.args[0].data)
            self.assertEqual(body["response_format"],{"type":"json_object"})
            self.assertNotIn("test-key",json.dumps(body))
