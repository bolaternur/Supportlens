"""SupportLens operator workspace. Run: python -m streamlit run app.py"""
from datetime import datetime, timedelta
from html import escape
import json
import uuid
import pandas as pd
import streamlit as st
from supportlens.data import ACTIONS, FIELDS, PRIORITIES, STATUSES, TOPICS
from supportlens.engine import AIError, Settings, safety_rule
from supportlens.storage import ConflictError, LOCAL_TZ, Store

st.set_page_config(page_title="SupportLens · Поддержка", page_icon="◉", layout="wide")
st.markdown("""<style>
.block-container{max-width:1460px;padding:3.8rem 2.2rem 2rem}h1{font-size:1.85rem!important;letter-spacing:-.04rem}h2{font-size:1.25rem!important}h3{font-size:1.05rem!important}
[data-testid="stSidebar"]{border-right:1px solid #DFE7E4}[data-testid="stMetric"]{background:#FFF;border:1px solid #DEE8E4;border-radius:12px;padding:12px 17px}
[data-testid="stMetricValue"]{font-size:1.7rem}.brand{font-size:25px;font-weight:800;color:#176B58;letter-spacing:-.7px}.eyebrow{font-size:11px;color:#6A7F77;letter-spacing:1.5px}
.pill{display:inline-block;border-radius:6px;padding:4px 9px;font-weight:600;font-size:12px;margin-right:7px}.critical{color:#A0233A;background:#FCE7EB}.high{color:#925B0C;background:#FFF0CE}.normal{color:#176B58;background:#E2F3EB}.low{color:#355F8B;background:#E5EDF9}
.client{white-space:pre-wrap;overflow-wrap:anywhere;border-left:3px solid #176B58;padding:12px 15px;border-radius:6px;background:#EDF3F1;font-size:14px}.muted{font-size:12px;color:#6D7B79}
[data-testid="stMain"]{overflow-x:hidden}button{white-space:normal!important}
html,body,[data-testid="stApp"]{font-family:"Segoe UI",sans-serif;color:#182F35}
[data-testid="stSidebar"]{background:#F6FAFA;border-right:1px solid #DAE6E7}
.brand{color:#0D6468;display:flex;align-items:center;gap:10px;font-size:25px;margin-bottom:7px}
.brand-mark{display:grid;place-items:center;width:35px;height:35px;background:#0D6468;color:white;border-radius:11px;font-size:22px}
.eyebrow{letter-spacing:.2px;font-size:12px;color:#52686F;margin-bottom:26px;padding-left:45px}
[class*="st-key-nav_"] button{justify-content:flex-start;min-height:48px;padding:10px 15px;border:1px solid transparent;border-radius:9px;background:transparent;color:#40585F;gap:12px}
[class*="st-key-nav_"] button:hover{background:#E7F0F1;border-color:#D3E4E5;color:#0D6468}
[class*="st-key-nav_"] button[kind="primary"]{background:#DBEEED;color:#084E53;border-color:#DBEEED;border-left:4px solid #0D6B70;font-weight:700;padding-left:12px}
[class*="st-key-nav_"] button>div>span{justify-content:flex-start;width:100%}
button:focus-visible{outline:3px solid #137A82!important;outline-offset:3px!important}
button p{white-space:normal!important;overflow:visible!important;text-overflow:clip!important;line-height:1.35!important}
[data-testid="stSidebarUserContent"]>div>[data-testid="stVerticalBlock"]{min-height:calc(100dvh - 150px)}
[data-testid="stSidebarUserContent"] [data-testid="stLayoutWrapper"]:has(>.st-key-ai_indicator){margin-top:auto}
.st-key-ai_indicator{border-top:1px solid #D9E6E7;padding-top:18px}
.ai-status{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:600}.status-dot{width:8px;height:8px;border-radius:50%;background:#839597}.status-dot.ready{background:#15756A}.status-dot.error{background:#BA3E48}
.basis{font-size:12px;line-height:1.5;background:#F2F7F7;border:1px solid #DDE9E9;padding:8px 11px;border-radius:8px;margin:7px 0;color:#40585F}.basis strong{color:#0D6468}
@media(max-width:760px){.block-container{padding:3.6rem 1rem 1rem}h1{font-size:1.5rem!important}[data-testid="stMetric"]{padding:7px 10px}[data-testid="stMetricValue"]{font-size:1.35rem}.helper-note{display:none}}
</style>""", unsafe_allow_html=True)

store = Store()
store.initialize()
settings = Settings.from_env()
articles = store.articles()
ai = store.ai_state(settings)
if "pending_nav" in st.session_state:
    st.session_state["nav"] = st.session_state.pop("pending_nav")
if "pending_view" in st.session_state:
    st.session_state["view"] = st.session_state.pop("pending_view")
st.session_state.setdefault("view", "queue")
st.session_state.setdefault("nav", "Обращения")
article_route = st.query_params.get("article")
if article_route in {a["id"] for a in articles} and st.session_state.get("article_route") != article_route:
    st.session_state["article_route"] = article_route
    st.session_state["nav"] = "База знаний"
    st.session_state["pending_kb"] = article_route
st.session_state.setdefault("buffers", {})
st.session_state.setdefault("intake_token", str(uuid.uuid4()))
MODE = {"demo": "Без AI · локальные правила", "ai": "Ответ создан моделью", "fallback": "Ошибка AI · резервный режим", "pending": "Ожидает обработки", "manual": "Локальная обработка"}
COLOR = dict(zip(PRIORITIES, ["critical", "high", "normal", "low"]))
EVENT = {"analysis": "Обращение обработано", "approved": "Ответ утверждён", "demo_approved": "Начальное демоутверждение", "draft_saved": "Черновик сохранён", "manual_classification": "Классификация изменена; ответ сохранён", "status": "Рабочий статус изменён", "analysis_conflict": "Устаревший анализ отклонён"}


def when(value):
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def flash(text):
    st.session_state["flash"] = text


def navigate(section, view="queue", id=None):
    st.session_state["pending_nav"], st.session_state["pending_view"] = section, view
    if id is not None:
        st.session_state["ticket_id"] = id
    st.rerun()


def nav_changed():
    if st.session_state["nav"] == "Обращения":
        st.session_state["view"] = "queue"


with st.sidebar:
    st.markdown('<div class="brand"><span class="brand-mark">◉</span>SupportLens</div><div class="eyebrow">Рабочее место оператора</div>', unsafe_allow_html=True)
    for name, key, icon in [("Обращения","tickets","inbox"),("База знаний","knowledge","menu_book"),("Аналитика","analytics","bar_chart"),("Подключение AI","connection","settings_input_component")]:
        if st.button(name, key="nav_"+key, icon=f":material/{icon}:", type="primary" if st.session_state["nav"] == name else "secondary", width="stretch"):
            navigate(name)
    with st.container(key="ai_indicator"):
        label = {"not_configured":"Деморежим","unverified":"Деморежим","ready":"Подключён","error":"Ошибка подключения"}[ai["state"]]
        st.markdown(f'<div class="ai-status"><span class="status-dot {ai["state"]}"></span>AI · {label}</div>', unsafe_allow_html=True)
        st.caption({"not_configured":"Ключ не настроен · локальные правила","unverified":"Подключение ещё не проверено","ready":"Подтверждено реальным ответом модели","error":"Последний запрос не прошёл. Доступен повтор."}[ai["state"]])
        st.caption("Qala Market · Казахстан\n\nВымышленный магазин. Ответы проверяет оператор; отправка не выполняется.")

section = st.session_state["nav"]

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))


source_render_count = 0


def source_panel(sources, snapshots, compact=True):
    global source_render_count
    source_render_count += 1
    catalog = {a["id"]: a for a in snapshots}
    live = {a["id"]: a for a in articles}
    if not sources:
        st.caption("В этом ответе нет ссылок на правила магазина.")
    for id in dict.fromkeys(s["id"] for s in sources):
        a = catalog.get(id, live.get(id, {}))
        with st.expander(f"{id} · {a.get('title','Источник')} · v{a.get('version',1)}", expanded=not compact):
            if id in live and a.get("version", 1) != live[id]["version"]:
                st.caption("Статья обновлена. Здесь сохранён снимок, использованный для этого ответа.")
            for s in sources:
                if s["id"] == id:
                    st.text(s["quote"])
            if id in live and st.button("Открыть статью " + id, key=f"source_{source_render_count}_{id}"):
                st.session_state["pending_kb"] = id
                navigate("База знаний")
            if id in live:
                st.caption(f"[Постоянная ссылка на {id}](?article={id})")


def basis_panel(parts, sources=None, snapshots=None, show_text=False):
    if not parts:
        parts = [{"kind":"kb" if sources else "check", "explanation":"Сохранённый ответ по источникам." if sources else "Для этой версии нет подтверждённых источников.", "source_ids":list(dict.fromkeys(s["id"] for s in sources or []))}]
    for part in parts:
        title = {"kb":"По базе знаний","general":"Общий ответ ИИ","check":"Нужна проверка"}[part["kind"]]
        explanation = part.get("explanation", "")
        if part["kind"] == "general":
            explanation = "Это общее объяснение, не правило компании. " + explanation
        st.markdown(f'<div class="basis"><strong>{title}</strong><br>{escape(explanation)}</div>', unsafe_allow_html=True)
        if show_text and part.get("text"):
            st.write(part["text"])
    if sources:
        source_panel(sources, snapshots or [])


def rows_table(tickets, key, height=440):
    frame = pd.DataFrame([{"№": f"SL-{t['id']:04d}", "Обращение": t["message"].replace("\n", " ")[:110], "Тема": t["topic"],
        "Приоритет": t["priority"], "Статус": t["status"], "Создано": when(t["created_at"])} for t in tickets])
    colors = {"критический": "background-color:#FCE7EB;color:#A0233A", "высокий": "background-color:#FFF0CE;color:#925B0C", "обычный": "background-color:#E2F3EB;color:#176B58", "низкий": "background-color:#E5EDF9;color:#355F8B"}
    event = st.dataframe(frame.style.map(lambda v: colors[v], subset=["Приоритет"]), hide_index=True, width="stretch", height=height,
        column_config={"№": st.column_config.TextColumn(width="small"), "Обращение": st.column_config.TextColumn(width="large"), "Приоритет": st.column_config.TextColumn(width="small")},
        on_select="rerun", selection_mode=["single-row", "single-cell"], key=key)
    if event.selection.rows:
        navigate("Обращения", "card", tickets[event.selection.rows[0]]["id"])
    if event.selection.cells:
        navigate("Обращения", "card", tickets[event.selection.cells[0][0]]["id"])


def editor_buffer(ticket):
    id = ticket["id"]
    buffers = st.session_state["buffers"]
    if id not in buffers:
        buffers[id] = {"text": ticket["draft"], "revision": ticket["revision"], "draft_revision": ticket["draft_revision"], "language": ticket["language"]}
    if st.session_state.pop(f"reset_buffer_{id}", False):
        buffers[id] = {"text": ticket["draft"], "revision": ticket["revision"], "draft_revision": ticket["draft_revision"], "language": ticket["language"]}
        st.session_state[f"editor_{id}"] = ticket["draft"]
    return buffers[id]


def remember_editor(id):
    st.session_state["buffers"][id]["text"] = st.session_state[f"editor_{id}"]


def process_ticket(ticket, language=None):
    with st.status("Обработка обращения", expanded=True) as status:
        st.write("Обращение уже сохранено в SQLite.")
        store.process(ticket["id"], settings, expected_revision=ticket["revision"], language=language, progress=lambda stage: st.write(stage))
        status.update(label="Анализ и черновик сохранены", state="complete", expanded=False)
    st.session_state[f"reset_buffer_{ticket['id']}"] = True


def assistant_panel(ticket, buffer):
    id = ticket["id"]
    with st.container(border=True):
        st.subheader("✧ AI-помощник")
        st.caption(f"Контекст только SL-{id:04d}. Предложения применяются по выбору оператора.")
        enabled = settings.enabled
        if not enabled:
            st.info("Подключите и проверьте модель, чтобы задавать вопросы и менять ответ с помощью AI.")
            if st.button("Перейти к подключению AI", key=f"setup_{id}", width="stretch"):
                navigate("Подключение AI")
        question = None
        quick = ["Почему такой приоритет?", "Какая инструкция подходит?", "Что ещё нужно уточнить?", "Сделай ответ короче", "Подготовь ответ на казахском"]
        qcols = st.columns(2)
        for index, text in enumerate(quick):
            if qcols[index % 2].button(text, key=f"quick_{id}_{index}", disabled=not enabled, width="stretch"):
                question = text
        with st.form(f"chat_{id}", clear_on_submit=True):
            free = st.text_input("Вопрос помощнику", placeholder="По обращению или общий вопрос, например 2 + 2", disabled=not enabled, max_chars=2000)
            asked = st.form_submit_button("Спросить AI", disabled=not enabled, width="stretch")
        if asked:
            question = free
        failed = st.session_state.get(f"failed_question_{id}")
        if failed:
            st.error(failed["error"])
            if st.button("Повторить запрос", key=f"retry_{id}", disabled=not enabled):
                question = failed["question"]
        if question is not None:
            if buffer["text"] != ticket["draft"]:
                st.warning("Сначала сохраните ручные правки: помощник использует сохранённый черновик.")
            else:
                try:
                    with st.spinner("Модель изучает обращение и источники…"):
                        store.ask_assistant(id, question, settings, expected_revision=ticket["revision"])
                    st.session_state.pop(f"failed_question_{id}", None)
                    st.rerun()
                except (AIError, ValueError) as exc:
                    st.session_state[f"failed_question_{id}"] = {"question":question,"error":str(exc)}
                    st.rerun()
        messages = store.conversation(id)
        if messages:
            with st.container(height=330):
                for item in messages[-8:]:
                    with st.chat_message(item["role"]):
                        st.write(item["text"])
                        result = item["result"]
                        if item["role"] == "assistant":
                            basis_panel(result.get("response_parts", []), result.get("sources", []), result.get("citation_snapshots", []))
                        if result.get("suggestion"):
                            with st.expander("Предложение для клиента", expanded=True):
                                st.write(result["suggestion"])
                                if st.button("Применить к черновику", key=f"apply_{item['id']}", disabled=buffer["text"] != ticket["draft"]):
                                    try:
                                        store.apply_suggestion(id, item["id"], ticket["revision"])
                                        st.session_state[f"reset_buffer_{id}"] = True
                                        flash("Предложение применено как черновик. Утверждение выполняется отдельно.")
                                        st.rerun()
                                    except ValueError as exc:
                                        st.error(str(exc))
        else:
            st.caption("Здесь появится отдельный диалог по этому обращению. При отключённом AI ответы модели не имитируются.")


def render_card(ticket):
    id = ticket["id"]
    if st.button("← К очереди"):
        st.session_state["view"] = "queue"
        st.rerun()
    st.title(f"SL-{id:04d} · Обращение")
    st.markdown(f'<span class="pill {COLOR[ticket["priority"]]}">{escape(ticket["priority"])}</span><span class="pill normal">{escape(ticket["topic"])}</span><span class="muted">{escape(ticket["status"])} · {when(ticket["created_at"])} · {MODE[ticket["mode"]]}</span>', unsafe_allow_html=True)
    st.markdown('<div class="client">' + escape(ticket["message"]) + '</div>', unsafe_allow_html=True)
    if ticket["error"]:
        st.warning(ticket["error"] + " Ответ создан резервными правилами, не моделью.")
    buffer = editor_buffer(ticket)
    left, right = st.columns([1.35, 1], gap="medium")
    with left:
        with st.container(border=True):
            st.subheader("Ответ клиенту")
            basis_panel(ticket["response_parts"], ticket["sources"], ticket["source_snapshots"])
            st.caption("Основания относятся к сохранённому варианту. Ручные правки проверяет оператор.")
            if ticket["status"] == "утверждено":
                st.success("Утверждён " + when(ticket["approved_at"]) + ". Отправка не выполнялась.")
            if buffer["revision"] != ticket["revision"]:
                st.warning("В базе есть более новая версия. Текст редактора сохранён отдельно; сравните его с последней версией.")
                a, b = st.columns(2)
                if a.button("Загрузить сохранённую версию", key=f"load_{id}"):
                    st.session_state[f"reset_buffer_{id}"] = True
                    st.rerun()
                if b.button("Оставить мой текст после сравнения", key=f"rebase_{id}"):
                    buffer["revision"] = ticket["revision"]
                    st.rerun()
                with st.expander("Последний сохранённый ответ"):
                    st.write(ticket["draft"])
            key = f"editor_{id}"
            if key not in st.session_state:
                st.session_state[key] = buffer["text"]
            st.text_area("Редактируемый ответ", key=key, height=245, max_chars=12000, on_change=remember_editor, args=(id,))
            save, approve = st.columns(2)
            save_clicked = save.button("Сохранить черновик", disabled=ticket["mode"] == "pending", width="stretch")
            approve_clicked = approve.button("Утвердить ответ", type="primary", disabled=ticket["mode"] == "pending", width="stretch")
            if save_clicked or approve_clicked:
                try:
                    store.save_draft(id, buffer["text"], approve=approve_clicked, expected_revision=buffer["revision"],
                                     sources=buffer.get("sources"), snapshots=buffer.get("snapshots"), response_parts=buffer.get("response_parts"), question_parts=buffer.get("question_parts"), question_type=buffer.get("question_type"))
                    st.session_state[f"reset_buffer_{id}"] = True
                    flash("Ответ утверждён и сохранён." if approve_clicked else "Черновик сохранён.")
                    st.rerun()
                except ValueError as exc:
                    st.error(str(exc))
            with st.expander("Создать новый черновик / сменить язык", expanded=ticket["mode"] == "pending"):
                language = st.selectbox("Язык ответа", ["Русский", "Қазақша"], index=int(ticket["language"] == "kk"), key=f"language_{id}")
                accept = st.checkbox("Сохранить текущие правки и заменить черновик новой генерацией", key=f"regenerate_ack_{id}")
                if st.button("Сгенерировать новый ответ", key=f"regenerate_{id}", disabled=not accept and bool(ticket["draft"])):
                    try:
                        current = ticket
                        if buffer["text"] != ticket["draft"]:
                            store.save_draft(id, buffer["text"], expected_revision=buffer["revision"])
                            current = store.ticket(id)
                        process_ticket(current, "kk" if language == "Қазақша" else "ru")
                        flash("Новый ответ подготовлен. Предыдущие версии сохранены.")
                        st.rerun()
                    except ValueError as exc:
                        st.error(str(exc))
    with right:
        assistant_panel(ticket, buffer)
    with st.expander("Анализ, найденные сведения и рекомендации"):
        st.write("**Тема:** " + ticket["topic_reason"])
        st.write("**Приоритет:** " + ticket["priority_reason"])
        risk = safety_rule(ticket["message"])
        if risk and PRIORITIES.index(ticket["priority"]) > PRIORITIES.index(risk["priority"]):
            st.warning("Ручной приоритет ниже правила риска: " + risk["reason"])
        known = {FIELDS.get(k,k): v for k,v in ticket["known_fields"].items() if v}
        if known:
            st.write("**Из сообщения клиента (не подтверждённые статусы):**")
            for field, value in known.items():
                st.write(f"• {field.capitalize()}: {value}")
        st.write("**Нужно уточнить:** " + (", ".join(FIELDS[f] for f in ticket["missing_fields"]) or "Дополнительные сведения не требуются для этого вопроса."))
        st.info(ticket["action"] + " · " + ticket["operator_notes"])
        if ticket["elapsed_ms"] is not None:
            st.caption(f"Обработка: {ticket['elapsed_ms']:.1f} мс · версия записи {ticket['revision']}")
        st.caption("Классификация меняется отдельно от ответа. Новая генерация — отдельное действие; текст и утверждение здесь сохраняются.")
        with st.form(f"classification_{id}"):
            a,b = st.columns(2)
            topic = a.selectbox("Тема обращения", TOPICS, index=TOPICS.index(ticket["topic"]))
            priority = b.selectbox("Приоритет обращения", PRIORITIES, index=PRIORITIES.index(ticket["priority"]))
            changed = st.form_submit_button("Применить исправление")
        if changed:
            try:
                store.correct(id, topic, priority, ticket["revision"])
                buffer["revision"] = store.ticket(id)["revision"]
                flash("Классификация исправлена. Черновик, утверждение и ручные правки сохранены.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
        with st.form(f"status_{id}"):
            status = st.selectbox("Рабочий статус", STATUSES[:-1], index=STATUSES[:-1].index(ticket["status"]) if ticket["status"] in STATUSES[:-1] else 1)
            status_changed = st.form_submit_button("Сохранить статус")
        if status_changed:
            try:
                store.change_status(id, status, ticket["revision"])
                buffer["revision"] = store.ticket(id)["revision"]
                flash("Статус сохранён.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    with st.expander(f"Источники и подтверждающие фрагменты · {len(set(s['id'] for s in ticket['sources']))}"):
        source_panel(ticket["sources"], ticket["source_snapshots"])
    with st.expander("Версии ответа и история действий"):
        versions = store.versions(id)
        if versions:
            chosen = st.selectbox("Сохранённая версия", [v["id"] for v in versions], format_func=lambda vid: next(f"{when(v['created_at'])} · {v['status']} · v{v['revision']}" for v in versions if v["id"] == vid))
            version = next(v for v in versions if v["id"] == chosen)
            st.write(version["answer"])
            source_panel(json.loads(version["sources"]), json.loads(version["snapshots"]))
            if st.button("Загрузить эту версию в редактор", key=f"restore_{id}"):
                buffer["text"] = version["answer"]
                buffer["sources"] = json.loads(version["sources"])
                buffer["snapshots"] = json.loads(version["snapshots"])
                buffer["response_parts"] = json.loads(version["response_parts"])
                buffer["question_parts"] = json.loads(version["question_parts"])
                buffer["question_type"] = version["question_type"]
                st.session_state[f"pending_editor_{id}"] = version["answer"]
                st.rerun()
        for event in store.audit(id)[:15]:
            st.caption(when(event["created_at"]) + " · " + EVENT.get(event["event"], event["event"]))
        with st.expander("Технические данные событий"):
            for event in store.audit(id)[:15]:
                st.json(event["payload"], expanded=False)


if section == "Подключение AI":
    st.title("Подключение AI")
    st.write("Добавьте доступ к выбранной модели. Проверка выполняется только по вашей кнопке.")
    (st.success if ai["state"] == "ready" else st.info)(ai["message"])
    if ai.get("checked_at"):
        st.caption("Последняя проверка / ответ: " + when(ai["checked_at"]))
    st.write(f"**Провайдер:** {settings.provider} · **Модель:** {settings.model} · **Формат:** {settings.response_format}")
    st.caption("Ключ в интерфейсе не отображается. Наличие ключа не подтверждает работу модели.")
    if st.button("Проверить соединение", type="primary", disabled=not settings.enabled):
        with st.spinner("Проверяем модель и совместимость JSON-формата…"):
            result = store.check_ai(settings)
        flash(result["message"])
        st.rerun()
    with st.container(border=True):
        st.subheader("Настройка на этом компьютере")
        st.write("1. В папке проекта скопируйте `.env.example` в `.env`.\n2. В локальном редакторе заполните ключ, адрес API и модель.\n3. Сохраните файл и нажмите «Обновить настройки», затем «Проверить соединение».")
        st.code("AI_API_KEY=ваш_ключ\nAI_BASE_URL=https://api.openai.com/v1\nAI_MODEL=gpt-4o-mini\nAI_PROVIDER=openai-compatible\nAI_RESPONSE_FORMAT=json_schema\nAI_TIMEOUT_SECONDS=20\nAI_TOTAL_TIMEOUT_SECONDS=60", language="dotenv")
        st.write("Используется совместимый Chat Completions API. Адрес задаётся до `/chat/completions`. Если провайдер поддерживает только JSON mode, выберите `AI_RESPONSE_FORMAT=json_object`: серверная проверка структуры и фактов остаётся включённой.")
        st.caption("Удалённый API — HTTPS. HTTP разрешён только для уже доступного локального сервера. Для локального сервера, которому ключ не нужен, задайте его документированное фиктивное значение; работа подтвердится только реальным ответом.")
        if st.button("Обновить настройки"):
            st.rerun()
    st.info("Без подключения работают локальные правила и короткие двуязычные шаблоны. Проверки с подставными ответами не подтверждают доступ к настоящей модели.")

elif section == "Обращения":
    if st.session_state["view"] == "card":
        ticket = store.ticket(st.session_state["ticket_id"])
        if ticket:
            pending = st.session_state.pop(f"pending_editor_{ticket['id']}", None)
            if pending is not None:
                st.session_state[f"editor_{ticket['id']}"] = pending
            render_card(ticket)
        else:
            st.info("Обращение не найдено. Вернитесь к очереди.")
    elif st.session_state["view"] == "new":
        if st.button("← К очереди"):
            st.session_state["view"] = "queue"
            st.rerun()
        st.title("Новое обращение")
        st.caption("Для демонстрации используйте вымышленные данные. Обращение сохраняется до начала внешнего запроса.")
        token = st.session_state["intake_token"]
        with st.form(f"new_{token}"):
            message = st.text_area("Сообщение клиента", max_chars=6000, height=150, placeholder="Списали дважды по 18000 тенге, номера заказа у меня нет")
            add = st.form_submit_button("Добавить и обработать", type="primary")
        if add:
            try:
                id = store.create_ticket(message, token)
                current = store.ticket(id)
                if current["mode"] == "pending":
                    process_ticket(current)
                st.session_state["intake_token"] = str(uuid.uuid4())
                flash(f"Обращение SL-{id:04d} сохранено.")
                navigate("Обращения", "card", id)
            except ValueError as exc:
                st.error(str(exc))
    else:
        h,b = st.columns([3,1])
        h.title("Обращения")
        if b.button("＋ Новое обращение", type="primary", width="stretch"):
            st.session_state["view"] = "new"
            st.rerun()
        all_tickets = store.tickets()
        c1,c2,c3 = st.columns(3)
        c1.metric("В работе", sum(t["status"] != "утверждено" for t in all_tickets))
        c2.metric("Требуют внимания", sum(t["status"] != "утверждено" and t["priority"] in PRIORITIES[:2] for t in all_tickets))
        c3.metric("Без инструкции", sum(t["mode"] != "pending" and not t["sources"] and t["question_type"] != "general" for t in all_tickets))
        a,b,c,d = st.columns([1,1,1,1.3])
        topic = a.selectbox("Тема", ["Все темы", *TOPICS])
        priority = b.selectbox("Приоритет", ["Все приоритеты", *PRIORITIES])
        status = c.selectbox("Статус", ["Все статусы", *STATUSES])
        sort = d.selectbox("Сортировка", ["Сначала срочные в работе", "Сначала новые", "Сначала старые"])
        search = st.text_input("Поиск по сообщению или номеру", placeholder="Найти обращение…")
        tickets = store.tickets(None if topic == "Все темы" else topic, None if priority == "Все приоритеты" else priority, None if status == "Все статусы" else status, sort={"Сначала срочные в работе":"risk","Сначала новые":"new","Сначала старые":"old"}[sort])
        if search:
            tickets = [t for t in tickets if search.casefold() in t["message"].casefold() or search.casefold() in f"sl-{t['id']:04d}" or search == str(t["id"])]
        st.caption(f"Найдено {len(tickets)} · Нажмите на строку, чтобы открыть карточку. Приоритеты обозначены цветом и текстом.")
        if tickets:
            rows_table(tickets, "queue_" + str(hash((topic,priority,status,sort,search))))
        else:
            st.info("Нет обращений по этим фильтрам. Измените фильтры или создайте новое обращение.")

elif section == "База знаний":
    st.title("База знаний")
    st.caption("Правила вымышленного магазина. При изменении статьи прошлые ответы сохраняют снимки источников.")
    if "pending_kb" in st.session_state:
        st.session_state["kb_search"] = st.session_state.pop("pending_kb")
        st.session_state["kb_topic"] = "Все темы"
    query = st.text_input("Найти статью", placeholder="Название, текст или KB-003", key="kb_search")
    topic = st.selectbox("Раздел базы", ["Все темы", *TOPICS], key="kb_topic")
    for a in articles:
        if (topic == "Все темы" or a["topic"] == topic) and (not query or query.casefold() in (a["id"]+a["title"]+a["body"]).casefold()):
            with st.expander(f"{a['id']} · {a['title']} · v{a['version']}", expanded=bool(query)):
                st.write(a["body"])
                st.caption("Действие: " + a["action"])
    with st.expander("＋ Создать или изменить статью"):
        choice = st.selectbox("Статья для редактирования", ["Новая статья", *[a["id"] for a in articles]])
        a = next((a for a in articles if a["id"] == choice), None)
        with st.form("article_" + choice + "_" + str(a["version"] if a else 0)):
            id = st.text_input("Идентификатор статьи", value=a["id"] if a else "KB-016", disabled=bool(a))
            title = st.text_input("Название статьи", value=a["title"] if a else "")
            atopic = st.selectbox("Тема статьи", TOPICS, index=TOPICS.index(a["topic"]) if a else 0)
            body = st.text_area("Текст статьи", value=a["body"] if a else "", height=220)
            keywords = st.text_input("Ключевые слова через запятую", value=", ".join(a["keywords"]) if a else "")
            fields = st.multiselect("Сведения для проверки", list(FIELDS), default=a["fields"] if a else [], format_func=lambda f: FIELDS[f])
            action = st.selectbox("Рекомендуемое действие", ACTIONS, index=ACTIONS.index(a["action"]) if a else 0)
            write_article = st.form_submit_button("Сохранить статью", type="primary")
        if write_article:
            try:
                version = store.save_article(id,title,atopic,body,[k.strip() for k in keywords.split(",") if k.strip()],fields,action,a["version"] if a else None)
                flash(f"Статья {id} сохранена, версия {version}.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))

else:
    st.title("Аналитика поддержки")
    a,b = st.columns([1.4,1])
    population = a.selectbox("Набор данных", ["Все обращения", "Только добавленные оператором", "Только демонстрационные"])
    period = b.selectbox("Период", ["За всё время", "Сегодня", "Последние 7 дней", "Выбрать даты"])
    today = datetime.now(LOCAL_TZ).date()
    start,end = None,None
    if period == "Сегодня":
        start,end = today,today
    elif period == "Последние 7 дней":
        start,end = today-timedelta(days=6),today
    elif period == "Выбрать даты":
        dates = st.date_input("Диапазон дат", value=(today-timedelta(days=9),today), format="DD.MM.YYYY")
        if len(dates) == 2:
            start,end = dates
        else:
            st.info("Выберите начало и конец периода.")
            st.stop()
    m = store.metrics({"Все обращения":None,"Только добавленные оператором":0,"Только демонстрационные":1}[population], start,end)
    a,b,c,d = st.columns(4)
    a.metric("Всего обращений",m["total"])
    b.metric("Без инструкции",len(m["no_instruction"]))
    c.metric("Утверждено ответов",f"{m['approval_share']:.1%}" if m["processed"] else "—")
    d.metric("Среднее время AI",f"{m['mean_ai_ms']/1000:.2f} с" if m["mean_ai_ms"] is not None else "Нет вызовов")
    st.caption(f"Утверждено {m['approved']} из {m['processed']} обработанных. Это доля утверждения, а не точность модели. Демоутверждения включены только в соответствующий набор.")
    a,b,c = st.columns(3)
    a.metric("Успешные AI-обработки",m["successful_ai_runs"])
    b.metric("Локальные обработки",m["local_runs"])
    c.metric("Ошибки AI → резерв",m["failed_ai_runs"])
    left,right = st.columns(2)
    with left:
        st.subheader("Темы обращений")
        st.bar_chart(pd.DataFrame({"Тема":list(m["topics"]),"Количество":list(m["topics"].values())}).set_index("Тема"),color="#176B58",horizontal=True)
        selected_topic = st.selectbox("Открыть обращения по теме", ["Выберите тему",*TOPICS])
        if selected_topic != "Выберите тему":
            for t in [t for t in m["tickets"] if t["topic"] == selected_topic][:15]:
                if st.button(f"SL-{t['id']:04d} · {t['message'][:65]}",key=f"topic_link_{t['id']}"):
                    navigate("Обращения","card",t["id"])
    with right:
        st.subheader("Приоритеты")
        st.bar_chart(pd.DataFrame({"Приоритет":list(m["priorities"]),"Количество":list(m["priorities"].values())}).set_index("Приоритет"),color="#C68B35",horizontal=True)
        selected_priority = st.selectbox("Открыть обращения по приоритету",["Выберите приоритет",*PRIORITIES])
        if selected_priority != "Выберите приоритет":
            for t in [t for t in m["tickets"] if t["priority"] == selected_priority][:15]:
                if st.button(f"SL-{t['id']:04d} · {t['message'][:65]}",key=f"priority_link_{t['id']}"):
                    navigate("Обращения","card",t["id"])
    st.subheader("Динамика поступления · UTC+5")
    if m["by_day"]:
        st.line_chart(pd.DataFrame({"Дата":pd.to_datetime(list(m["by_day"])),"Обращения":list(m["by_day"].values())}).set_index("Дата"),color="#176B58")
    else:
        st.info("Нет данных за период. Измените даты или добавьте обращение.")
    st.subheader("Без найденной инструкции")
    if m["no_instruction"]:
        rows_table(m["no_instruction"],"missing_"+str(hash((population,period,str(start),str(end)))),270)
    else:
        st.caption("В этом наборе нет обработанных обращений без источников.")
    with st.expander("Методика расчёта"):
        st.write("Обращения отбираются по дате создания, UTC+5. Обработки — по дате запуска в том же периоде для этих обращений. Повторные запуски учитываются отдельно. Время измеряется таймером. Среднее AI включает генерацию и проверку фактов; ошибки показаны отдельно. Черновики и источники сохраняются в SQLite. Неизмеренные показатели не рассчитываются.")
