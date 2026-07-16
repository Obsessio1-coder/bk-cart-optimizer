import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bk_api import fetch_menu

RESTAURANTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bk_all_menus", "__restaurants.json")

with open(RESTAURANTS_FILE, encoding="utf-8") as f:
    restaurants = json.load(f)

existing = set()
cache_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bk_all_menus")
for fn in os.listdir(cache_dir):
    if fn.startswith("menu_") and fn.endswith(".json"):
        rid = fn.replace("menu_", "").replace(".json", "")
        existing.add(int(rid))

todo = [r for r in restaurants if r["id"] not in existing]
print(f"Всего: {len(restaurants)}, уже есть: {len(existing)}, нужно скачать: {len(todo)}")

ok = 0
fail = 0
for i, r in enumerate(todo):
    rid = r["id"]
    name = r.get("name", "?")[:40]
    try:
        fetch_menu(rid)
        ok += 1
        print(f"[{i+1}/{len(todo)}] OK {rid} — {name}")
    except Exception as e:
        fail += 1
        print(f"[{i+1}/{len(todo)}] FAIL {rid} — {name}: {e}")
    time.sleep(0.3)

print(f"\nГотово. Загружено: {ok}, ошибок: {fail}")
