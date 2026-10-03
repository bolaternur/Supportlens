"""Run with: python -m streamlit run app.py"""
from datetime import datetime
from html import escape

import pandas as pd
import streamlit as st

from supportlens.data import FIELDS, PRIORITIES, STATUSES, TOPICS
from supportlens.engine import Settings, safety_rule
from supportlens.storage import LOCAL_TZ, Store

st.set_page_config(page_title="SupportLens · Поддержка", page_icon="◉", layout="wide")
st.markdown("""<style>
.block-container {max-width: 1440px; padding-top: 4rem; padding-bottom: 3rem;}
h1 {font-size: 2.15rem !important; letter-spacing: -.055rem;}
h2 {font-size: 1.4rem !important;}
h3 {font-size: 1.12rem !important;}
[data-testid="stSidebar"] {border-right: 1px solid #DFE6E8;}
[data-testid="stMetric"] {background: white; border: 1px solid #E0E7E8; padding: 18px; border-radius: 12px;}
[data-testid="stMetricLabel"] {color: #60717A;}
.brand {font-size: 26px; font-weight: 800; letter-spacing: -1px; margin: 4px 0; color: #176B58;}
.eyebrow {font-size: 11px; letter-spacing: 1.8px; color: #617780; text-transform: uppercase; margin: 0 0 18px;}
.pill {display:inline-block; border-radius:7px; font-size:13px; font-weight:650; padding:5px 10px; margin-right:7px;}
.critical {color:#9C2439; background:#FCE6EC;} .high {color:#915607; background:#FFF0CD;}
.normal {color:#176B58; background:#E0F3EC;} .low {color:#3D6285; background:#E5EDF9;}
.muted {font-size:13px; color:#647780;}
.client-message {white-space:pre-wrap; overflow-wrap:anywhere; background:#F3F6F8; border-left:3px solid #176B58; padding:18px; border-radius:8px; font-size:15px;}
</style>""", unsafe_allow_html=True)

store = Store()
store.initialize()
settings = Settings.from_env()
articles = store.articles()

MODE_LABELS = {"demo": "Демо без AI · правила и шаблон", "ai": "AI · проверенные источники",
               "fallback": "Ошибка API · локальные правила", "manual": "Исправлено оператором · локальный поиск",
               "pending": "Ожидает обработки"}
COLORS = {"критический": "critical", "высокий": "high", "обычный": "normal", "низкий": "low"}


def local_time(value):
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).strftime("%d.%m.%Y %H:%M")


def toast_next(message):
    st.session_state["flash"] = message


def ticket_table(tickets):
    return pd.DataFrame([{
        "№": f"SL-{t['id']:04d}", "Обращение": t["message"].replace("\n", " ")[:110],
        "Тема": t["topic"], "Приоритет": t["priority"], "Статус": t["status"],
        "Создано · UTC+5": local_time(t["created_at"]), "Данные": "демо" if t["is_demo"] else "добавлено оператором",
    } for t in tickets])


TABLE_COLUMNS = {
    "№": st.column_config.TextColumn(width="small"),
    "Обращение": st.column_config.TextColumn(width="medium"),
    "Тема": st.column_config.TextColumn(width="small"),
    "Приоритет": st.column_config.TextColumn(width="small"),
    "Статус": st.column_config.TextColumn(width="medium"),
    "Создано · UTC+5": st.column_config.TextColumn(width="medium"),
    "Данные": st.column_config.TextColumn(width="small"),
}


with st.sidebar:
    st.markdown('<div class="brand">◉ SupportLens</div><div class="eyebrow">AI Support Assistant</div>', unsafe_allow_html=True)
    section = st.radio("Рабочее пространство", ["Обращения", "База знаний", "Аналитика"], label_visibility="collapsed")
    st.divider()
    st.caption("МАГАЗИН")
    st.markdown("**Qala Market**  \nПоддержка интернет-магазина · Казахстан")
    st.info("Вымышленный магазин. Все правила и начальные обращения — демонстрационные.")
    if settings.enabled:
        st.success("API настроен")
        st.caption("Доступ к модели проверяется при обработке нового обращения. Начальные данные обработаны локально.")
    else:
        st.warning("Демо без AI")
        st.caption("Классификация по правилам, поиск по ключевым словам и шаблон с цитатами. Языковая модель не подключена.")
    st.divider()
    st.caption("Оператор проверяет и утверждает ответ. Сообщения клиентам не отправляются.")
    st.caption("Время отображается в UTC+5.")

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))


def render_card(ticket):
    id = ticket["id"]
    st.divider()
    title, status = st.columns([3, 1])
    title.subheader(f"SL-{id:04d} · Карточка обращения")
    status.caption(f"Статус: {ticket['status']}")
    st.markdown(f'<span class="pill {COLORS[ticket["priority"]]}">{escape(ticket["priority"].capitalize())}</span>'
                f'<span class="pill normal">{escape(ticket["topic"].capitalize())}</span>'
                f'<span class="muted">{local_time(ticket["created_at"])} · {"Демонстрационное обращение" if ticket["is_demo"] else "Добавлено оператором"}</span>', unsafe_allow_html=True)
    st.caption(MODE_LABELS[ticket["mode"]])
    if ticket["error"]:
        st.warning(ticket["error"] + " Обращение сохранено. Использован безопасный деморежим без ответа модели.")
    left, right = st.columns([1, 1], gap="large")
    with left:
        st.markdown("### Сообщение клиента")
        st.markdown('<div class="client-message">' + escape(ticket["message"]) + '</div>', unsafe_allow_html=True)
        st.markdown("### Анализ обращения")
        st.write("**Тема:** " + (ticket["topic_reason"] or "Ещё не определена."))
        st.write("**Приоритет:** " + (ticket["priority_reason"] or "Ещё не определён."))
        if ticket["elapsed_ms"] is not None:
            st.caption(f"Последняя обработка: {ticket['elapsed_ms']:.3f} мс" +
                       (f" · API: {ticket['ai_ms']:.3f} мс" if ticket["ai_ms"] is not None else " · без вызова модели"))
        risk = safety_rule(ticket["message"])
        if risk and PRIORITIES.index(ticket["priority"]) > PRIORITIES.index(risk["priority"]):
            st.warning("Оператор понизил приоритет относительно правила риска: " + risk["reason"])
        with st.expander("Исправить классификацию"):
            st.caption("При исправлении поиск и черновик обновятся локально. Предыдущее утверждение будет снято; его текст останется в истории.")
            with st.form(f"classification_{id}_{ticket['updated_at']}"):
                topic = st.selectbox("Тема обращения", TOPICS, index=TOPICS.index(ticket["topic"]))
                priority = st.selectbox("Приоритет обращения", PRIORITIES, index=PRIORITIES.index(ticket["priority"]))
                changed = st.form_submit_button("Применить исправление")
            if changed:
                store.correct(id, topic, priority)
                toast_next("Классификация исправлена, источники и черновик обновлены.")
                st.rerun()
        if st.button("Повторить анализ" if ticket["mode"] != "pending" else "Обработать обращение", key=f"reprocess_{id}", disabled=ticket["status"] == "утверждено"):
            with st.spinner("Анализируем и проверяем источники…"):
                store.process(id, settings)
            toast_next("Обращение обработано.")
            st.rerun()
        with st.expander("Изменить рабочий статус"):
            statuses = STATUSES[:-1]
            new_status = st.selectbox("Рабочий статус", statuses, index=statuses.index(ticket["status"]) if ticket["status"] in statuses else 1, key=f"status_{id}")
            if st.button("Сохранить статус", key=f"status_save_{id}"):
                store.change_status(id, new_status)
                toast_next("Статус сохранён. Если ответ был утверждён, утверждение снято.")
                st.rerun()
    with right:
        st.markdown("### Источники из базы знаний")
        if not ticket["sources"]:
            st.warning("Подтверждённая инструкция не найдена. " + ticket["action"] + ".")
        source_ids = list(dict.fromkeys(s["id"] for s in ticket["sources"]))
        catalog = {a["id"]: a for a in articles}
        for source_id in source_ids:
            a = catalog[source_id]
            with st.expander(f"{source_id} · {a['title']}", expanded=True):
                for source in ticket["sources"]:
                    if source["id"] == source_id:
                        st.text(source["quote"])
                st.caption("Цитаты совпадают с полными абзацами сохранённой статьи. Полный текст — в разделе «База знаний».")
        st.markdown("### Что уточнить или проверить")
        if ticket["missing_fields"]:
            for field in ticket["missing_fields"]:
                st.write("• " + FIELDS[field].capitalize())
        else:
            st.caption("Дополнительные поля не выбраны. Сверьте факты конкретного заказа перед утверждением.")
        st.info("Рекомендуемое действие: **" + ticket["action"] + "**")
    st.markdown("### Черновик ответа")
    st.caption("Проверьте применимость цитат и данные клиента. Не добавляйте неподтверждённые обещания или действия. Утверждение только сохраняет ответ.")
    if ticket["status"] == "утверждено":
        st.success("Ответ утверждён " + local_time(ticket["approved_at"]) + ". Отправка клиенту не выполнялась.")
    if ticket["mode"] != "pending":
        with st.form(f"draft_{id}_{ticket['updated_at']}"):
            answer = st.text_area("Редактируемый ответ", value=ticket["draft"], height=300, max_chars=12000)
            c1, c2 = st.columns([1, 3])
            save = c1.form_submit_button("Сохранить черновик")
            approve = c2.form_submit_button("Утвердить ответ", type="primary")
        if save or approve:
            try:
                store.save_draft(id, answer, approve=approve)
                toast_next("Ответ утверждён и сохранён. Клиенту ничего не отправлено." if approve else "Черновик сохранён.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    else:
        st.info("Обработайте обращение, чтобы подготовить черновик.")
    with st.expander("История действий"):
        names = {"analysis": "Обработка", "manual_classification": "Исправление классификации", "approved": "Утверждение ответа",
                 "demo_approved": "Демонстрационное утверждение", "draft_saved": "Сохранение черновика", "status": "Изменение статуса"}
        for event in store.audit(id)[:20]:
            st.caption(local_time(event["created_at"]) + " · " + names.get(event["event"], event["event"]))
            st.json(event["payload"], expanded=False)


if section == "Обращения":
    st.caption("РАБОЧЕЕ ПРОСТРАНСТВО / ОЧЕРЕДЬ")
    st.title("Каждое обращение — под контролем")
    st.write("Определите риск, проверьте источники и подготовьте ответ клиенту.")
    all_tickets = store.tickets()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Всего обращений", len(all_tickets))
    c2.metric("Требуют внимания", sum(t["status"] != "утверждено" and t["priority"] in PRIORITIES[:2] for t in all_tickets))
    c3.metric("Без инструкции", sum(t["mode"] != "pending" and not t["sources"] for t in all_tickets))
    c4.metric("Ответов утверждено", sum(t["status"] == "утверждено" for t in all_tickets))
    with st.expander("＋ Новое обращение", expanded=not all_tickets):
        st.caption("Вставьте сообщение на русском, казахском или со смешением языков. Для демо не используйте реальные персональные данные.")
        with st.form("new_ticket", clear_on_submit=True):
            message = st.text_area("Сообщение клиента", placeholder="За заказ 5170 деньги списали два раза. Проверьте, пожалуйста.", max_chars=6000, height=110)
            submit = st.form_submit_button("Добавить и обработать", type="primary")
        if submit:
            try:
                id = store.create_ticket(message)
                st.session_state["selected_ticket"] = id
                with st.spinner("Обращение сохранено. Готовим анализ и черновик…"):
                    store.process(id, settings)
                toast_next(f"Обращение SL-{id:04d} добавлено и обработано.")
                st.rerun()
            except ValueError as exc:
                st.error(str(exc))
    st.subheader("Очередь обращений")
    f1, f2, f3, f4 = st.columns([1, 1, 1, 1.5])
    ft = f1.selectbox("Тема", ["Все темы", *TOPICS])
    fp = f2.selectbox("Приоритет", ["Все приоритеты", *PRIORITIES])
    fs = f3.selectbox("Статус", ["Все статусы", *STATUSES])
    search = f4.text_input("Поиск по сообщению или номеру", placeholder="Например, двойное списание")
    tickets = store.tickets(None if ft == "Все темы" else ft, None if fp == "Все приоритеты" else fp, None if fs == "Все статусы" else fs)
    if search:
        tickets = [t for t in tickets if search.casefold() in t["message"].casefold() or search.casefold() in f"sl-{t['id']:04d}" or search == str(t["id"])]
    st.caption(f"Найдено: {len(tickets)} · 🔴 критический · 🟠 высокий · 🟢 обычный · 🔵 низкий")
    if tickets:
        df = ticket_table(tickets)
        def highlight_priority(value):
            styles = {"критический": "background-color:#FCE6EC;color:#9C2439", "высокий": "background-color:#FFF0CD;color:#915607",
                      "обычный": "background-color:#E0F3EC;color:#176B58", "низкий": "background-color:#E5EDF9;color:#3D6285"}
            return styles[value]
        st.dataframe(df.style.map(highlight_priority, subset=["Приоритет"]), column_config=TABLE_COLUMNS,
                     hide_index=True, width="stretch", height=310)
        ids = [t["id"] for t in tickets]
        selected = st.session_state.get("selected_ticket", ids[0])
        if selected not in ids:
            selected = ids[0]
        by_id = {t["id"]: t for t in tickets}
        # A new widget key makes a newly added ticket the selected card immediately.
        chosen = st.selectbox("Открыть карточку", ids, index=ids.index(selected),
                              format_func=lambda id: f"SL-{id:04d} · {by_id[id]['message'][:85]}",
                              key=f"card_picker_{selected}_{ft}_{fp}_{fs}_{search}")
        st.session_state["selected_ticket"] = chosen
        render_card(store.ticket(chosen))
    else:
        st.info("Нет обращений по выбранным фильтрам.")

elif section == "База знаний":
    st.caption("РАБОЧЕЕ ПРОСТРАНСТВО / ИСТОЧНИКИ")
    st.title("База знаний Qala Market")
    st.write("15 статей с проверяемыми цитатами. Все правила вымышлены и предназначены для демонстрации.")
    c1, c2 = st.columns([2, 1])
    query = c1.text_input("Найти статью", placeholder="Название, идентификатор или фраза из текста")
    topic = c2.selectbox("Раздел базы", ["Все темы", *TOPICS])
    filtered = [a for a in articles if (topic == "Все темы" or a["topic"] == topic) and
                (not query or query.casefold() in (a["id"] + a["title"] + a["body"]).casefold())]
    st.caption(f"Статей найдено: {len(filtered)}")
    for a in filtered:
        with st.expander(f"{a['id']} · {a['title']}", expanded=bool(query)):
            st.caption(a["topic"].upper() + " · ВЫМЫШЛЕННЫЕ ПРАВИЛА")
            for paragraph in a["body"].split("\n\n"):
                st.write(paragraph)
            st.info("Действие: " + a["action"])
    if not filtered:
        st.info("Статьи не найдены. Не подставляйте неподтверждённые правила в ответ.")

else:
    st.caption("РАБОЧЕЕ ПРОСТРАНСТВО / АНАЛИТИКА")
    st.title("Что происходит в поддержке")
    st.write("Показатели из сохранённых обращений и журналов обработки. Демообращения можно исключить.")
    population = st.selectbox("Набор данных", ["Все обращения", "Только добавленные оператором", "Только демонстрационные"])
    demo = {"Все обращения": None, "Только добавленные оператором": 0, "Только демонстрационные": 1}[population]
    m = store.metrics(demo)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Всего обращений", m["total"])
    c2.metric("Без найденной инструкции", len(m["no_instruction"]))
    c3.metric("Утверждено черновиков", f"{m['approval_share']:.1%}" if m["processed"] else "—")
    c4.metric("Среднее время AI", f"{m['mean_ai_ms'] / 1000:.3f} с" if m["mean_ai_ms"] is not None else "Нет вызовов")
    st.caption(f"Доля утверждённых: {m['approved']} из {m['processed']} обработанных обращений. "
               f"Успешных AI-обработок: {m['successful_ai_runs']}. Начальные 8 утверждений — демонстрационные сохранённые ответы.")
    left, right = st.columns(2, gap="large")
    with left:
        with st.container(border=True):
            st.subheader("Распределение по темам")
            st.bar_chart(pd.DataFrame({"Тема": list(m["topics"]), "Обращения": list(m["topics"].values())}).set_index("Тема"), color="#176B58", horizontal=True)
    with right:
        with st.container(border=True):
            st.subheader("Распределение по приоритетам")
            priority_df = pd.DataFrame({"Приоритет": list(m["priorities"]), "Обращения": list(m["priorities"].values())})
            st.bar_chart(priority_df.set_index("Приоритет"), color="#D09339", horizontal=True)
    with st.container(border=True):
        st.subheader("Динамика поступления · UTC+5")
        if m["by_day"]:
            df = pd.DataFrame({"Дата": pd.to_datetime(list(m["by_day"])), "Обращения": list(m["by_day"].values())})
            st.line_chart(df.set_index("Дата"), color="#176B58")
        else:
            st.info("Пока нет данных для графика.")
    st.subheader("Контроль обработки")
    c1, c2, c3 = st.columns(3)
    c1.metric("Запусков обработки", m["processing_runs"])
    c2.metric("Среднее время всей обработки", f"{m['mean_processing_ms']:.3f} мс" if m["mean_processing_ms"] is not None else "—")
    c3.metric("Ошибок AI с переходом на демо", m["failed_ai_runs"])
    st.caption("Время измеряется таймером при реальной обработке, включая поиск и проверку. Для AI учитывается время двух API-запросов и проверки результатов; повторные запуски учитываются отдельно.")
    if m["mean_failed_ai_ms"] is not None:
        st.caption(f"Среднее время неуспешной попытки API и перехода на правила: {m['mean_failed_ai_ms']:.3f} мс.")
    st.subheader("Обращения без инструкции")
    if m["no_instruction"]:
        st.dataframe(ticket_table(m["no_instruction"]), column_config=TABLE_COLUMNS, hide_index=True, width="stretch")
    else:
        st.info("Обращений без инструкции в выбранном наборе нет.")
    with st.expander("Как рассчитываются показатели"):
        st.write("Тема, приоритет, дата и статус берутся из SQLite. Без инструкции — обработанные обращения с пустым списком источников. "
                 "Доля утверждённых — текущие утверждённые ответы / обработанные обращения; повторный анализ и правки снимают утверждение. "
                 "Среднее время AI — успешные обработки с mode=ai из журнала; ошибки API показаны отдельно. "
                 "Среднее время всей обработки включает локальные и API-запуски. Загрузка базы и ручное редактирование в него не входят.")
