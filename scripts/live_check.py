"""Explicit live verification using only fictional tickets. Never prints credentials."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from supportlens.engine import AIError, Settings, call_api
from supportlens.storage import Store

parser = argparse.ArgumentParser()
parser.add_argument("scenario", choices=["payment", "delivery", "kazakh", "assistant"])
args = parser.parse_args()
store = Store()
store.initialize()
settings = Settings.from_env()

def traced_transport(settings, schema, payload):
    stage = "classification" if "topic" in schema["properties"] else "fact_check" if "supported" in schema["properties"] else "generation"
    print(json.dumps({"stage": stage, "event": "request"}))
    value = call_api(settings, schema, payload)
    print(json.dumps({"stage": stage, "event": "response_received"}))
    if "segments" in value:
        print(json.dumps({"evidence_keys":value["sources"], "segment_sources":[s["source_ids"] for s in value["segments"]], "fictional_reply":[s["text"] for s in value["segments"]]}, ensure_ascii=False))
    return value
examples = {
    "payment": "[Вымышленное обращение для проверки Groq] За заказ 5170 деньги списали два раза по 18000 тенге сегодня. Проверьте, пожалуйста.",
    "delivery": "[Вымышленное обращение для проверки Groq] Сколько стоит доставка в Алматы?",
    "kazakh": "Алматыға жеткізу қанша тұрады? Бұл тек тексеруге арналған ойдан шығарылған өтініш.",
}
if args.scenario == "assistant":
    id = store.create_ticket(examples["payment"], "live-v2-payment")
    ticket = store.ticket(id)
    try:
        result = store.ask_assistant(id, "Сделай ответ короче", settings, ticket["revision"])
    except AIError as exc:
        print(json.dumps({"ticket":id,"error":str(exc),"category":exc.category},ensure_ascii=False))
        sys.exit(1)
    print(json.dumps({"ticket": id, "assistant_answer": result["text"], "suggestion": result["suggestion"], "source_ids": sorted({s["id"] for s in result["sources"]})}, ensure_ascii=False))
else:
    id = store.create_ticket(examples[args.scenario], "live-v2-" + args.scenario)
    result = store.process(id, settings, transport=traced_transport, expected_revision=store.ticket(id)["revision"])
    print(json.dumps({"ticket": id, **{k: result[k] for k in ("mode", "topic", "priority", "language", "known_fields", "missing_fields", "draft", "operator_notes", "error", "ai_ms")},
                      "source_ids": sorted({s["id"] for s in result["sources"]})}, ensure_ascii=False))
