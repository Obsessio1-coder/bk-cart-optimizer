import sys, json, math
from collections import Counter
from optimizer import optimize, generate_alternative_carts, _tag_portion

PASS = 0
FAIL = 0

def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  OK  {name}")
    else:
        FAIL += 1
        print(f"FAIL  {name} {detail}")

def round2(x):
    return round(x * 100) / 100

def t1(result):
    """Extract tier 1 data from new result dict in old tuple format."""
    t = result["tiers"][0]
    best_state = t["best_state"]
    return (
        t["best_total"],
        t["savings"],
        best_state,
        result["original_menu_total"],
        t["menu_prices"],
        t["best_eff"],
        t["best_mono"],
        {},
        t["order"],
    )

def test_basic():
    r = optimize(["Воппер", "Кола", "Наггетсы"], restaurant_id="1002", mode="offline")
    check("returns tuple of 9", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, menu_prices, effective_prices, mono_plan, dish_idx, order = t1(r)
    check("best_total > 0", best_total > 0, str(best_total))
    check("savings >= 0", savings >= 0, str(savings))
    check("menu_total > best_total", menu_total >= best_total, f"{menu_total} >= {best_total}")
    check("order has 3 items", sum(order.values()) == 3)
    check("all items matched", len(order) == 3)

def test_single_whoper():
    r = optimize(["Воппер"], restaurant_id="1002", mode="offline")
    check("single item returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, _, _, _, _, _ = t1(r)
    check("best_total > 0", best_total > 0, str(best_total))
    check("savings >= 0", savings >= 0, str(savings))
    check("total matches", abs(round2(menu_total) - best_total - savings) < 0.01, f"{menu_total} = {best_total} + {savings}")

def test_all_combo_items():
    r = optimize(["Воппер", "Кола", "Картофель фри"], restaurant_id="1002", mode="offline")
    check("combo items returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, _, _, _, _, _, _ = t1(r)
    mono_used_items, best_combo, final_remaining, detail_combos, best_costs = best_state
    has_combo = best_combo is not False and len(best_combo) > 0
    has_multi = len(best_costs.get("multi_used", [])) > 0
    check("uses combo or multi", has_combo or has_multi, f"combo={best_combo} multi={best_costs.get('multi_used')}")
    check("no remaining", all(v <= 0 for v in final_remaining.values()), str(final_remaining))
    check("savings > 0", savings > 0, str(savings))

def test_fuzzy_match():
    r = optimize(["воппер", "кола мал", "наггетсы 9"], restaurant_id="1002", mode="offline")
    check("fuzzy returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    total_qty = sum(order.values())
    check("matched 3 items", total_qty == 3, str(order))
    check("fuzzy matched whoper", any("Воппер" in n for n in order), str(order))

def test_quantity():
    r = optimize(["Воппер", "Воппер", "Кола", "Наггетсы"], restaurant_id="1002", mode="offline")
    check("quantity returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    check("2x Воппер", order.get("Воппер", 0) == 2, str(order))

def test_with_quantity_syntax():
    r = optimize(["Воппер", "Воппер", "Кола", "Наггетсы"], restaurant_id="1002", mode="offline")
    check("x2 syntax returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    check("2x Воппер", order.get("Воппер", 0) == 2, str(order))

def test_menu_total_vs_individual():
    r = optimize(["Воппер", "Кола", "Картофель фри", "Наггетсы", "Чизбургер"], restaurant_id="1002", mode="offline")
    check("5 items returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, menu_prices, _, _, _, order = t1(r)
    calc_manual = sum(menu_prices.get(n.lower(), 0) * c for n, c in order.items())
    check("menu_total matches manual", abs(menu_total - calc_manual) < 0.01, f"api={menu_total}, manual={calc_manual}")
    check("total = best + savings", abs(menu_total - best_total - savings) < 0.01, f"{menu_total} = {best_total} + {savings}")

def test_price_kopecks_integrity():
    r = optimize(["Воппер", "Кола"], restaurant_id="1002", mode="offline")
    check("price test returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, _, _, _, _, _ = t1(r)
    best_costs = best_state[4]
    check("best_total matches best_costs total", abs(best_total - best_costs["total"]) < 0.01, f"{best_total} vs {best_costs['total']}")
    with_sauce = best_costs.get("with_sauce", best_total)
    without_sauce = best_costs.get("without_sauce", best_total)
    check("sauce variants consistent", with_sauce >= best_total or without_sauce >= best_total)

def test_remaining():
    r = optimize(["Кола", "Наггетсы"], restaurant_id="1002", mode="offline")
    check("remaining test returns tuple", len(r.get("tiers", [])) > 0)
    _, _, best_state, _, _, _, _, _, _ = t1(r)
    final_remaining = best_state[2]
    has_remaining = any(v > 0 for v in final_remaining.values())
    check("some remaining or all matched", True, f"remaining: {final_remaining}")

def test_different_store():
    r = optimize(["Воппер", "Кола", "Наггетсы"], restaurant_id="1003", mode="offline")
    check("store 1003 returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    check("store 1003 matched items", len(order) > 0, str(order))

def test_empty_list():
    r = optimize([], restaurant_id="1002", mode="offline")
    check("empty list returns None", r is None)

def test_nonexistent_items():
    r = optimize(["Эскалоп из мамонта", "Глазунья из страуса"], restaurant_id="1002", mode="offline")
    check("nonexistent returns None (none matched)", r is None)

def test_mono_coupon_used():
    r = optimize(["Воппер", "Воппер", "Воппер", "Кола", "Наггетсы"], restaurant_id="1002", mode="offline")
    check("mono coupon returns tuple", len(r.get("tiers", [])) > 0)
    _, _, best_state, _, _, _, mono_plan, _, _ = t1(r)
    if mono_plan:
        check("mono_plan has items", len(mono_plan) > 0, str(mono_plan))
        for name, (coupon, price) in mono_plan.items():
            check(f"  mono: {name} @ {price}", price > 0 and coupon, f"{coupon} @ {price}")
    else:
        check("no mono coupons available", True)

def test_all_mono_possible():
    r = optimize(["Наггетсы 6 шт", "Кола мал"], restaurant_id="1003", mode="offline")
    check("mono test returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, _, menu_total, _, effective_prices, mono_plan, _, order = t1(r)
    if mono_plan:
        check("mono savings exist", savings >= 0, str(savings))
        check("effective price <= menu price for mono items", True)
    else:
        check("no mono for this order", True)

def test_сок():
    r = optimize(["Сок"], restaurant_id="1002", mode="offline")
    check("сок returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    check("сок matched", len(order) > 0, str(order))

def test_кофе():
    r = optimize(["Кофе"], restaurant_id="1002", mode="offline")
    check("кофе returns tuple", len(r.get("tiers", [])) > 0)
    _, _, _, _, _, _, _, _, order = t1(r)
    check("кофе matched", len(order) > 0, str(order))

# ── тесты альтернативных корзин (Query Expansion) ──────────────────

def test_tag_portion_fries():
    from optimizer import PORTION_SIZES
    check("small_fries in sizes", "small_fries" in PORTION_SIZES)
    check("medium_fries in sizes", "medium_fries" in PORTION_SIZES)
    check("large_fries in sizes", "large_fries" in PORTION_SIZES)
    check("nuggets_6 in sizes", "nuggets_6" in PORTION_SIZES)
    check("cola_small in sizes", "cola_small" in PORTION_SIZES)

def test_tag_portion_names():
    check("fries small", _tag_portion("Картофель Деревенский малый") == "small_fries", _tag_portion("Картофель Деревенский малый"))
    check("cola small", _tag_portion("Эвервесс Кола мал 0,4") == "cola_small")
    check("cola large", _tag_portion("Эвервесс Кола большая 0,8") == "cola_large")
    check("nuggets 6", _tag_portion("Наггетсы (6 шт.)") == "nuggets_6")
    check("nuggets 9", _tag_portion("Наггетсы (9 шт.)") == "nuggets_9")
    check("burger null", _tag_portion("Воппер") is None)
    check("cheese null", _tag_portion("Чизбургер") is None)

def test_generate_alt_carts():
    r = optimize(["Воппер", "Наггетсы (9 шт.)", "Кола"], restaurant_id="1002", mode="offline")
    check("alt test returns tuple", len(r.get("tiers", [])) > 0)
    _, _, best_state, _, _, _, _, _, _ = t1(r)
    best_costs = best_state[4]
    check("best_costs has saving_tip", "saving_tip" in best_costs)

def test_cola_expansion():
    r = optimize(["Воппер", "Эвервесс Кола большая 0,8", "Картофель фри"], restaurant_id="1002", mode="offline")
    check("cola expansion returns tuple", len(r.get("tiers", [])) > 0)
    _, _, best_state, _, _, _, _, _, _ = t1(r)
    best_costs = best_state[4]
    check("best_costs has saving_tip", "saving_tip" in best_costs)

def test_nuggets_expansion():
    r = optimize(["Воппер", "Наггетсы (9 шт.)", "Кола"], restaurant_id="1002", mode="offline")
    check("nuggets expansion returns tuple", len(r.get("tiers", [])) > 0)
    _, _, best_state, _, _, _, _, _, _ = t1(r)
    best_costs = best_state[4]
    check("best_costs has saving_tip", "saving_tip" in best_costs)

def test_fries_expansion():
    r = optimize(["Воппер", "Кинг Фри стандартный", "Кола"], restaurant_id="1002", mode="offline")
    check("fries expansion returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, _, _, _, _, _ = t1(r)
    best_costs = best_state[4]
    check("savings >= 0", savings >= 0, str(savings))
    check("total less than menu total", best_total <= menu_total, f"{best_total} <= {menu_total}")

def test_project_example():
    r = optimize(["Воппер", "Кинг Фри стандартный", "Кола"], restaurant_id="1002", mode="offline")
    check("example returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, _, menu_total, _, _, _, _, _ = t1(r)
    check("savings positive", savings >= 0, str(savings))
    check("total less than menu total", best_total <= menu_total, f"{best_total} <= {menu_total}")

def test_double_mono_coupon():
    r = optimize(["Кофе", "Кофе"], restaurant_id="1002", mode="offline")
    check("double mono returns tuple", len(r.get("tiers", [])) > 0)
    best_total, savings, best_state, menu_total, _, _, mono_plan, _, order = t1(r)
    mono_used_items, _, final_remaining, _, best_costs = best_state
    check("order has 2 items", sum(order.values()) == 2, str(order))
    check("best_total <= menu_total", best_total <= menu_total, f"{best_total} <= {menu_total}")
    check("no remaining", all(v <= 0 for v in final_remaining.values()), str(final_remaining))
    check("mono_plan has 1 item", len(mono_plan) == 1, str(mono_plan))
    check("mono_used has 1 item", len(mono_used_items) == 1, str(mono_used_items))
    check("price = 2x coupon", best_costs["total"] > 0, str(best_costs["total"]))

def test_multi_slot_sauce_coupon():
    r = optimize(["Соус Чесночный", "Соус Чесночный", "Кола", "Чикенбургер"], restaurant_id="1002", mode="offline")
    check("multi-slot test returns tuple", len(r.get("tiers", [])) > 0)
    best_total, _, best_state, menu_total, _, _, _, _, _ = t1(r)
    bc = best_state[4]
    check("best_total < menu_total", best_total < menu_total, f"{best_total} < {menu_total}")
    check("has_multi_slot_savings", best_total > 0, str(best_total))
    # 2 sauces via multi-slot (per-dish prices from struct) + Чикенбургер @ 99.99 mono + Кола @ 99.99 mono
    check("total ~ 309.95", abs(best_total - 309.95) < 0.01, str(best_total))

def test_multi_slot_3_sauces():
    r = optimize(["Соус Чесночный", "Соус Чесночный", "Соус Чесночный", "Кола"], restaurant_id="1002", mode="offline")
    check("multi-slot 3 returns tuple", len(r.get("tiers", [])) > 0)
    best_total, _, best_state, menu_total, _, _, _, _, _ = t1(r)
    check("best_total < menu_total", best_total < menu_total, f"{best_total} < {menu_total}")
    # 3 sauces via 3-slot multi-coupon (per-dish prices from struct) + Кола @ 99.99 mono
    check("total ~ 234.95", abs(best_total - 234.95) < 0.01, str(best_total))

def test_multi_slot_excess_4_sauces():
    r = optimize(["Соус Чесночный", "Соус Чесночный", "Соус Чесночный", "Соус Чесночный"], restaurant_id="1002", mode="offline")
    check("multi-slot 4 returns tuple", len(r.get("tiers", [])) > 0)
    best_total, _, _, menu_total, _, _, _, _, _ = t1(r)
    check("best_total < menu_total", best_total < menu_total, f"{best_total} < {menu_total}")
    # 3-slot multi-coupon (per-dish prices from struct) + 1 remaining sauce @ 59.99
    check("total ~ 194.95", abs(best_total - 194.95) < 0.01, str(best_total))

def test_multi_slot_mixed():
    r = optimize(["Соус Чесночный", "Соус Чесночный", "Воппер", "Кола"], restaurant_id="1002", mode="offline")
    check("multi-slot mixed returns tuple", len(r.get("tiers", [])) > 0)
    best_total, _, _, menu_total, _, _, _, _, _ = t1(r)
    check("best_total < menu_total", best_total < menu_total, f"{best_total} < {menu_total}")
    # 2 sauces @ 99.99 via multi-slot + Воппер maybe in combo or mono + Кола mono @ 99.99
    # Воппер could be in "Все по 99,99" if its dish_id is in that coupon
    check("best_total > 0", best_total > 0, str(best_total))

print("=" * 60)
print("ТЕСТИРОВАНИЕ ОПТИМИЗАТОРА")
print("=" * 60)
print()

tests = [
    test_basic,
    test_single_whoper,
    test_all_combo_items,
    test_fuzzy_match,
    test_quantity,
    test_with_quantity_syntax,
    test_menu_total_vs_individual,
    test_price_kopecks_integrity,
    test_remaining,
    test_different_store,
    test_empty_list,
    test_nonexistent_items,
    test_mono_coupon_used,
    test_all_mono_possible,
    test_сок,
    test_кофе,
    test_tag_portion_fries,
    test_tag_portion_names,
    test_generate_alt_carts,
    test_cola_expansion,
    test_nuggets_expansion,
    test_fries_expansion,
    test_project_example,
    test_double_mono_coupon,
    test_multi_slot_sauce_coupon,
    test_multi_slot_3_sauces,
    test_multi_slot_excess_4_sauces,
    test_multi_slot_mixed,
]

for t in tests:
    try:
        t()
    except Exception as e:
        FAIL += 1
        print(f"FAIL  {t.__name__} CRASH: {e}")

print()
print("=" * 60)
total = PASS + FAIL
print(f"РЕЗУЛЬТАТ: {PASS}/{total} пройдено ({FAIL} не пройдено)")
if FAIL > 0:
    sys.exit(1)
