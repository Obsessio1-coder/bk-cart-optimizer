import json

MENU_PATH = "bk_all_menus_combined.json"
STRUCT_PATH = "combo_structures.json"

with open(MENU_PATH, encoding="utf-8") as f:
    all_menus = json.load(f)
menu = all_menus["1002"]["menu"]["result"]

with open(STRUCT_PATH, encoding="utf-8") as f:
    struct = json.load(f)

# retail prices from restaurant dishes
retail = {}
for d in menu["dishes"]:
    mi = d["main_info"]
    retail[mi["id"]] = {"name": mi["name"], "price": mi["price"] / 100}

# combo price from restaurant
combo_id = "6457"
combo_name = "Комбо Воппер с сыром"
restaurant_price = None
for c in menu["combos"]:
    if c["main_info"]["id"] == 6457:
        restaurant_price = c["main_info"]["price"] / 100
        break

print("=" * 72)
print(f"КОМБО: {combo_name} (ID: {combo_id})")
print(f"Цена в ресторане 1002: {restaurant_price:.2f} руб")
print("=" * 72)

slots = struct["combos"][combo_id]["slots"]
print(f"\nВсего слотов: {len(slots)}\n")

slot_summaries = []
total_min_combo = 0.0
total_min_retail = 0.0
total_first_combo = 0.0

for si, slot in enumerate(slots):
    dishes = slot["dishes"]
    print(f"── Слот {si} (slot_id={slot['slot_id']}) ──")
    print(f"  {'Позиция':45s} {'В комбо':>8s} {'Розница':>8s} {'В меню':>8s}")
    print(f"  {'-'*45} {'-'*8} {'-'*8} {'-'*8}")

    min_combo = float("inf")
    min_retail = float("inf")
    min_combo_item = None
    min_retail_item = None
    first_item = None
    valid_count = 0

    for d in dishes:
        did = d["dish_id"]
        name = d.get("menu_name") or d["name"]
        combo_price = d["price"] / 100
        wo_combo = d.get("price_without_combo", 0) / 100
        in_menu = "✅" if did in retail else "❌"

        print(f"  {name:45s} {combo_price:>8.2f} {wo_combo:>8.2f} {in_menu:>8s}")

        if combo_price < min_combo:
            min_combo = combo_price
            min_combo_item = (name, combo_price, wo_combo, did, in_menu)

        if wo_combo > 0 and wo_combo < min_retail:
            min_retail = wo_combo
            min_retail_item = (name, combo_price, wo_combo, did, in_menu)

        if first_item is None:
            first_item = (name, combo_price, wo_combo, did, in_menu)

        valid_count += 1

    item = min_combo_item or first_item
    total_min_combo += item[1]
    total_min_retail += item[2] if item[2] > 0 else 0
    total_first_combo += first_item[1]

    slot_summaries.append({
        "first": first_item,
        "min_combo": min_combo_item,
        "min_retail": min_retail_item,
    })

    print()

print("=" * 72)
print("СВОДКА")
print("=" * 72)
print(f"\nЦена комбо в ресторане:          {restaurant_price:>8.2f} руб")
print(f"Сумма цен ПЕРВЫХ позиций слотов: {total_first_combo:>8.2f} руб")
print(f"Сумма min combo_price по слотам:  {total_min_combo:>8.2f} руб")
print(f"Сумма min розничных цен слотов:   {total_min_retail:>8.2f} руб")
print()

print("Минимальные combo_price по слотам:")
for si, s in enumerate(slot_summaries):
    item = s["min_combo"]
    print(f"  Слот {si}: {item[0]:40s} → {item[1]:.2f} руб (розница {item[2]:.2f})")

print()

if abs(total_min_combo - restaurant_price) < 0.01:
    print(f"✅ СУММА min_combo_price ({total_min_combo:.2f}) СОВПАДАЕТ с ценой ресторана ({restaurant_price:.2f})")
else:
    diff = restaurant_price - total_min_combo
    print(f"❌ НЕ СОВПАДАЕТ")
    print(f"   Разница: {diff:+.2f} руб")
    print(f"   Цена ресторана ({restaurant_price:.2f}) - Сумма min_combo ({total_min_combo:.2f}) = {diff:.2f}")

if abs(total_first_combo - restaurant_price) < 0.01:
    print(f"\n✅ СУММА ПЕРВЫХ позиций ({total_first_combo:.2f}) СОВПАДАЕТ с ценой ресторана ({restaurant_price:.2f})")
else:
    diff = restaurant_price - total_first_combo
    print(f"\n❌ СУММА ПЕРВЫХ позиций НЕ СОВПАДАЕТ")
    print(f"   Разница: {diff:+.2f} руб")
    print(f"   Первая позиция каждого слота, вероятно, НЕ является дефолтной.")

# Теперь проверим: может быть дефолт — это cheapest potato в слоте 1 и cheapest cola в слоте 2?
print()
print("=" * 72)
print("ПРОВЕРКА ГИПОТЕЗЫ: дефолт = cheapest товар с 'картошкой'/'колой'")
print("=" * 72)

POTATO_WORDS = {"фри", "картофель"}

for si, slot in enumerate(slots):
    dishes = slot["dishes"]
    potato_items = [d for d in dishes if any(w in (d.get("menu_name") or d["name"]).lower() for w in POTATO_WORDS)]
    if potato_items:
        cheapest = min(potato_items, key=lambda d: d["price"])
        name = cheapest.get("menu_name") or cheapest["name"]
        print(f"  Слот {si} (гарнир): cheapest potato = '{name}' @ {cheapest['price']/100:.2f} (розница {(cheapest.get('price_without_combo',0)/100):.2f})")
    cola_items = [d for d in dishes if "кола" in (d.get("menu_name") or d["name"]).lower() and not any(x in (d.get("menu_name") or d["name"]).lower() for x in ["кофе", "капучино", "латте", "чай"])]
    if cola_items and not potato_items:
        cheapest = min(cola_items, key=lambda d: d["price"])
        name = cheapest.get("menu_name") or cheapest["name"]
        print(f"  Слот {si} (напиток): cheapest cola = '{name}' @ {cheapest['price']/100:.2f} (розница {(cheapest.get('price_without_combo',0)/100):.2f})")
    if not potato_items and not cola_items:
        cheapest = min(dishes, key=lambda d: d["price"])
        name = cheapest.get("menu_name") or cheapest["name"]
        print(f"  Слот {si} (бургер): cheapest = '{name}' @ {cheapest['price']/100:.2f} (розница {(cheapest.get('price_without_combo',0)/100):.2f})")
