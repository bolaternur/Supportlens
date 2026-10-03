"""Real Streamlit widget runs against an isolated temporary database."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from supportlens.engine import ROOT
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
        self.app.sidebar.radio[0].set_value("База знаний").run()
        self.assert_no_errors()
        self.element("text_input", "Найти статью").set_value("KB-003").run()
        self.assert_no_errors()
        self.assertTrue(any("Возможное двойное списание" in e.label for e in self.app.expander))
        self.app.sidebar.radio[0].set_value("Аналитика").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "50")
        self.element("selectbox", "Набор данных").set_value("Только добавленные оператором").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "0")

    def test_new_ticket_edit_approve_and_dashboard(self):
        self.element("text_area", "Сообщение клиента").set_value("Деньги списали два раза за заказ 7731.")
        self.element("button", "Добавить и обработать").click().run()
        self.assert_no_errors()
        t = Store(self.db).tickets()[0]
        self.assertEqual(t["priority"], "высокий")
        self.assertEqual(self.element("selectbox", "Открыть карточку").value, t["id"])
        answer = self.element("text_area", "Редактируемый ответ").value + "\n\nУточните даты обеих операций."
        self.element("text_area", "Редактируемый ответ").set_value(answer)
        self.element("button", "Утвердить ответ").click().run()
        self.assert_no_errors()
        self.assertEqual(Store(self.db).ticket(t["id"])["approved_answer"], answer)
        self.assertEqual(Store(self.db).ticket(t["id"])["status"], "утверждено")
        self.app.sidebar.radio[0].set_value("Аналитика").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[0].value, "51")
        self.element("selectbox", "Набор данных").set_value("Только добавленные оператором").run()
        self.assert_no_errors()
        self.assertEqual(self.app.metric[2].value, "100.0%")
        # A fresh Streamlit session reads the saved answer without adding demo data.
        self.app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=60).run()
        self.assert_no_errors()
        self.assertEqual(Store(self.db).metrics()["total"], 51)
        self.assertEqual(self.element("text_area", "Редактируемый ответ").value, answer)

    def test_queue_filters_and_manual_correction(self):
        self.element("selectbox", "Тема").set_value("аккаунт").run()
        self.element("selectbox", "Приоритет").set_value("критический").run()
        self.assert_no_errors()
        selected = self.element("selectbox", "Открыть карточку").value
        self.assertEqual(Store(self.db).ticket(selected)["topic"], "аккаунт")
        self.element("selectbox", "Тема обращения").set_value("другое")
        self.element("selectbox", "Приоритет обращения").set_value("обычный")
        self.element("button", "Применить исправление").click().run()
        self.assert_no_errors()
        t = Store(self.db).ticket(selected)
        self.assertEqual((t["topic"], t["priority"], t["manual_override"]), ("другое", "обычный", 1))


if __name__ == "__main__":
    unittest.main()
