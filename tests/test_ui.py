"""Real Streamlit widget runs against an isolated temporary database."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from supportlens.engine import AIError, ROOT
from supportlens.storage import Store


class UITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Path(self.temp.name) / "ui.sqlite3"
        self.env = patch.dict(os.environ, {"SUPPORTLENS_DB": str(self.db), "AI_API_KEY": ""})
        self.env.start()
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def element(self, kind, label):
        return next(element for element in getattr(self.app, kind) if element.label == label)

    def assert_no_errors(self):
        self.assertEqual(len(self.app.exception), 0, str(self.app.exception))

    def test_all_three_sections_render_and_knowledge_search_works(self):
        self.assert_no_errors()
        self.element("button", "База знаний").click().run()
        self.assert_no_errors()
        self.element("text_input", "Найти статью").set_value("KB-003").run()
        self.assert_no_errors()
        self.assertTrue(any("Возможное двойное списание" in e.label for e in self.app.expander))
        self.element("button", "Аналитика").click().run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "50")
        self.element("selectbox", "Набор данных").set_value("Только добавленные оператором").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "0")

    def test_new_ticket_edit_approve_and_dashboard(self):
        self.element("button", "＋ Новое обращение").click().run()
        self.element("text_area", "Сообщение клиента").set_value("Деньги списали два раза за заказ 7731.")
        self.element("button", "Добавить и обработать").click().run()
        self.assert_no_errors()
        t = Store(self.db).tickets(sort="new")[0]
        self.assertEqual(t["priority"], "высокий")
        self.assertEqual(self.app.session_state["ticket_id"], t["id"])
        answer = self.element("text_area", "Редактируемый ответ").value + "\n\nУточните даты обеих операций."
        self.element("text_area", "Редактируемый ответ").set_value(answer)
        self.element("button", "Утвердить ответ").click().run()
        self.assert_no_errors()
        self.assertEqual(Store(self.db).ticket(t["id"])["approved_answer"], answer)
        self.assertEqual(Store(self.db).ticket(t["id"])["status"], "утверждено")
        self.element("button", "Аналитика").click().run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "51")
        self.element("selectbox", "Набор данных").set_value("Только добавленные оператором").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[2].value, "100.0%")
        # A fresh Streamlit session reads the saved answer without adding demo data.
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
        self.app.session_state["ticket_id"] = t["id"]
        self.app.session_state["view"] = "card"
        self.app.run()
        self.assert_no_errors()
        self.assertEqual(Store(self.db).metrics()["total"], 51)
        self.assertEqual(self.element("text_area", "Редактируемый ответ").value, answer)

    def test_queue_filters_and_manual_correction(self):
        self.element("selectbox", "Тема").set_value("аккаунт").run()
        self.element("selectbox", "Приоритет").set_value("критический").run()
        self.assert_no_errors()
        selected = Store(self.db).tickets(topic="аккаунт", priority="критический")[0]["id"]
        self.app.session_state["ticket_id"] = selected
        self.app.session_state["view"] = "card"
        self.app.run()
        self.assertEqual(Store(self.db).ticket(selected)["topic"], "аккаунт")
        self.element("selectbox", "Тема обращения").set_value("другое")
        self.element("selectbox", "Приоритет обращения").set_value("обычный")
        self.element("button", "Применить исправление").click().run()
        self.assert_no_errors()
        t = Store(self.db).ticket(selected)
        self.assertEqual((t["topic"], t["priority"], t["manual_override"]), ("другое", "обычный", 1))

    def test_unsaved_text_survives_navigation_and_manual_correction(self):
        id = Store(self.db).tickets()[0]["id"]
        self.app.session_state["ticket_id"] = id
        self.app.session_state["view"] = "card"
        self.app.run()
        original = Store(self.db).ticket(id)["draft"]
        text = original + "\nРучная правка, пока не сохранённая."
        self.element("text_area","Редактируемый ответ").set_value(text).run()
        self.element("selectbox","Тема обращения").set_value("другое")
        self.element("button","Применить исправление").click().run()
        self.assertEqual(self.element("text_area","Редактируемый ответ").value,text)
        self.element("button", "База знаний").click().run()
        self.element("button", "Обращения").click().run()
        self.element("selectbox","Тема").set_value("оплата").run()
        self.app.session_state["view"] = "card"
        self.app.run()
        self.assert_no_errors()
        self.assertEqual(self.element("text_area","Редактируемый ответ").value,text)
        self.assertEqual(Store(self.db).ticket(id)["draft"],original)

    def test_new_ticket_opens_even_when_queue_filters_exclude_it(self):
        self.element("selectbox","Тема").set_value("аккаунт").run()
        self.element("selectbox","Приоритет").set_value("критический").run()
        self.element("button","＋ Новое обращение").click().run()
        self.element("text_area","Сообщение клиента").set_value("Сколько стоит доставка в Алматы?")
        self.element("button","Добавить и обработать").click().run()
        self.assert_no_errors()
        t = Store(self.db).tickets(sort="new")[0]
        self.assertEqual(t["topic"],"доставка")
        self.assertEqual(self.app.session_state["ticket_id"],t["id"])
        self.assertEqual(self.app.session_state["view"],"card")

    def test_navigation_has_four_real_buttons_and_retains_selected_section(self):
        self.assertEqual(len(self.app.sidebar.radio),0)
        for label in ["База знаний","Аналитика","Подключение AI","Обращения"]:
            self.element("button",label).click().run()
            self.assert_no_errors()
            self.assertEqual(self.app.session_state["nav"],label)
            self.app.run()
            self.assertEqual(self.app.session_state["nav"],label)

    def test_general_without_key_is_explicit_and_not_sent_to_specialist(self):
        self.element("button","＋ Новое обращение").click().run()
        self.element("text_area","Сообщение клиента").set_value("Сколько будет 2 + 2?")
        self.element("button","Добавить и обработать").click().run()
        self.assert_no_errors()
        answer=self.element("text_area","Редактируемый ответ").value
        self.assertNotIn("специалист",answer)
        self.assertIn("подключение AI",answer)
        self.assertTrue(self.element("button","Спросить AI").disabled)

    def test_api_error_keeps_retry_enabled_and_preserves_draft(self):
        id=Store(self.db).tickets()[0]["id"]
        self.app.session_state["ticket_id"]=id
        self.app.session_state["view"]="card"
        with patch.dict(os.environ,{"AI_API_KEY":"test-only-key"}), patch("supportlens.storage.assistant_reply",side_effect=AIError("Ошибка API: HTTP 429. Повторите позже.","rate_limit")) as fake:
            self.app.run()
            before=self.element("text_area","Редактируемый ответ").value
            self.element("text_input","Вопрос помощнику").set_value("2 + 2?")
            self.element("button","Спросить AI").click().run()
            self.assert_no_errors()
            self.assertFalse(self.element("button","Повторить запрос").disabled)
            self.element("button","Повторить запрос").click().run()
            self.assert_no_errors()
            self.assertEqual(fake.call_count,2)
            self.assertEqual(self.element("text_area","Редактируемый ответ").value,before)
            self.assertEqual(Store(self.db).conversation(id),[])

    def test_real_article_deep_link_opens_only_existing_article(self):
        self.app.query_params["article"]="KB-007"
        self.app.run()
        self.assert_no_errors()
        self.assertEqual(self.app.session_state["nav"],"База знаний")
        self.assertEqual(self.element("text_input","Найти статью").value,"KB-007")
        self.assertTrue(any("KB-007" in e.label for e in self.app.expander))
        self.element("button","Обращения").click().run()
        self.assertEqual(self.app.session_state["nav"],"Обращения")


if __name__ == "__main__":
    unittest.main()
