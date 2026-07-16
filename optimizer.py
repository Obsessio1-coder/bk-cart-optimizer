import sys, re, argparse
from collections import Counter
from itertools import combinations

from bk_api import (
    load_data, build_menu_index, build_combo_prices, build_menu_id_set,
    normalize_combo, normalize_coupon, compute_effective_combo_price,
    validate_coupon_visibility, default_coupon_context,
    POTATO_WORDS, UPSELL_MATRIX, DEFAULT_UPCHARGE,
)

# ── парсинг аргументов ────────────────────────────────────────────────

def parse_args(args):
    joined = []
    for i, a in enumerate(args):
        if i > 0 and re.match(r"^\d+[.,]?\d*$", a):
            joined[-1] += " " + a
        else:
            joined.append(a)
    return joined

# ── стемминг / матчинг ────────────────────────────────────────────────

def _extract_quantity(name):
    m = re.search(r"\((\d+)\s*шт", name)
    return int(m.group(1)) if m else None

def _stem_dist(a, b):
    min_len = min(len(a), len(b), 6)
    if min_len < 4:
        return 0
    return sum(1 for i in range(min_len) if a[i] == b[i])

def _food_type(name_low):
    types = {
        "бургер": "burger", "воппер": "burger", "чизбургер": "burger",
        "гамбургер": "burger", "криспи": "burger", "ангус": "burger",
        "кола": "drink", "кока": "drink", "липтон": "drink",
        "фрустайл": "drink", "чай": "drink", "кофе": "drink",
        "наггетсы": "nuggets", "стрипсы": "nuggets",
        "фри": "fries", "картофель": "fries", "кольца": "fries",
        "соус": "sauce", "кетчуп": "sauce",
        "маффин": "dessert", "милкшейк": "dessert", "сандэй": "dessert",
        "пирожок": "dessert",
    }
    for k, v in types.items():
        if k in name_low:
            return v
    return None

def _name_match(user_name, slot_name):
    ul = user_name.lower()
    sl = slot_name.lower()
    if ul == sl:
        return True
    um = re.search(r"\((\d+)\s*шт", ul)
    sm = re.search(r"\((\d+)\s*шт", sl)
    u_core = re.sub(r"\([^)]*\)", "", ul).strip()
    s_core = re.sub(r"\([^)]*\)", "", sl).strip()
    if um and sm:
        uq = int(um.group(1))
        sq = int(sm.group(1))
        return u_core == s_core and sq >= uq
    if sm and not um:
        sw = set(re.findall(r"[а-яёa-z]+", s_core))
        uw = set(re.findall(r"[а-яёa-z]+", ul))
        return len(uw & sw) >= 1 and "наггет" in (uw & sw).pop() if (uw & sw) else False
    return False

def _fuzzy_match(user_name, candidates):
    ul = user_name.lower()
    for c in candidates:
        if c.lower() == ul:
            return c
    for c in candidates:
        if _name_match(user_name, c):
            return c
    utype = _food_type(ul)
    u_words = set(re.findall(r"[а-яёa-z0-9]+", ul))
    u_words.discard("шт")
    best_score, best = 0, None
    for c in candidates:
        cl = c.lower()
        s_words = set(re.findall(r"[а-яёa-z0-9]+", cl))
        s_words.discard("шт")
        stype = _food_type(cl)
        if utype and stype and utype != stype:
            continue
        if not (u_words <= s_words or s_words <= u_words):
            continue
        common = u_words & s_words
        if not common:
            continue
        score = _stem_dist(ul, cl) * 2 + len(common) * 3
        for kw in ("наггет", "кола", "воппер"):
            if kw in ul and kw in cl:
                score += 20
        # Penalty for extra words in candidate (e.g. "Evervess Cola" vs "Evervess Cola вишневая")
        if u_words < s_words:
            score -= len(s_words - u_words) * 10
        if s_words < u_words:
            score -= len(u_words - s_words) * 10
        # Bonus if user query is a prefix of the candidate (same start)
        if cl.startswith(ul):
            score += 15
        if score > best_score:
            best_score, best = score, c
    return best if best_score >= 12 else None

def match_item(query, idx):
    q = query.strip().lower()
    if q in idx:
        return idx[q]["name"]
    pref = []
    for k, v in idx.items():
        if k.startswith(q):
            m = re.search(r"\((\d+)\s*шт", k)
            qty = int(m.group(1)) if m else 999
            pref.append((v["combo_only"], qty, len(k), v["name"]))
    if pref:
        pref.sort(key=lambda x: (x[0], x[1], x[2]))
        return pref[0][3]
    if " " in q:
        qw = [w for w in re.findall(r"[а-яёa-z0-9]+", q) if len(w) >= 2]
        multi = []
        for k, v in idx.items():
            kw = re.findall(r"[а-яёa-z0-9]+", k)
            ok = sum(1 for qe in qw if any(
                min(len(qe), len(ke), 5) >= 3 and qe[:min(len(qe), len(ke), 5)] == ke[:min(len(qe), len(ke), 5)]
                for ke in kw))
            if ok >= len(qw):
                multi.append((len(k), k, v["name"]))
        if multi:
            multi.sort()
            return multi[0][2]
    if len(q) >= 3 and " " not in q:
        pat = re.compile(rf'\b{re.escape(q)}\b')
        ws = [(len(k), v["name"]) for k, v in idx.items() if pat.search(k)]
        if ws:
            ws.sort()
            return ws[0][1]
    if len(q) >= 6:
        subs = [(len(k), v["name"]) for k, v in idx.items() if q in k]
        if subs:
            subs.sort()
            return subs[0][1]
    res = []
    for k, v in idx.items():
        if len(q) >= 4 and len(k) >= 4:
            ml = min(len(q), len(k), 6)
            if q[:ml] == k[:ml]:
                res.append((len(k), k, v["name"]))
    if res:
        res.sort()
        return res[0][2]
    return None

# ── калькулятор стоимости выбранного комбо ────────────────────────────

def calculate_selected_combo_price(combo_item, selected_options):
    """Рассчитывает стоимость выбранной конфигурации комбо.

    Args:
        combo_item: нормализованный combo dict (из all_combos) с ключами
                    base_price_kopecks, groups.
        selected_options: dict {group_index: option_index} — какая опция
                          выбрана для каждой группы слотов.

    Returns:
        int: итоговая стоимость в копейках.
    """
    total_price = combo_item["base_price_kopecks"]
    for gi, group in enumerate(combo_item["groups"]):
        if gi not in selected_options:
            continue
        opt = group["options"][selected_options[gi]]
        total_price += opt["price_delta_kopecks"]
    return total_price

# ── главная функция ───────────────────────────────────────────────────

def optimize(wanted_list, restaurant_id="1002", mode="auto"):
    menu, struct, rid = load_data(rid=restaurant_id, mode=mode)
    dish_idx, menu_by_id, removed_menu, reasons = build_menu_index(menu, rid)
    combo_prices, combo_skipped = build_combo_prices(menu, rid)
    menu_id_set = build_menu_id_set(menu)

    # Матчинг заказа
    matched_names = []
    for item in wanted_list:
        name = match_item(item, dish_idx)
        if name:
            matched_names.append(name)
        else:
            print(f"НЕ НАЙДЕНО: {item}")
    if not matched_names:
        return

    order = Counter(matched_names)
    menu_prices = {}
    for n in order:
        e = dish_idx.get(n.lower())
        if e and e["price"] > 0 and not e.get("combo_only"):
            menu_prices[n.lower()] = e["price"]

    indiv_total = sum(menu_prices.get(n.lower(), 0) * c for n, c in order.items())

    print(f"\nРесторан {rid}")
    print(f"Отсеяно из меню: {removed_menu} (price_zero={reasons['price_zero']}, restricted={reasons['restricted']})")
    print(f"Комбо/купоны отброшены по lifecycle: {combo_skipped}")
    print("Заказ:")
    for n, c in sorted(order.items()):
        p = menu_prices.get(n.lower(), 0)
        co = " [только в комбо]" if dish_idx.get(n.lower(), {}).get("combo_only") else ""
        print(f"  {n} x{c} @ {p:.2f}{co}")
    print(f"  Без скидок: {indiv_total:.2f} руб")

    # ── Моно-купон: прямая цена из general_coupons ──
    mono_coupons = []
    coupon_ctx = default_coupon_context(restaurant_id)
    coupon_skipped = 0
    for entry in menu.get("general_coupons", []):
        ok, reason = validate_coupon_visibility(entry, coupon_ctx)
        if not ok:
            coupon_skipped += 1
            print(f"  [COUPON SKIP] {entry.get('main_info', {}).get('name', '?')!r}: {reason}")
            continue
        mi = entry["main_info"]
        coupon_id = mi["id"]
        code = entry.get("code", "")
        # Строгая сверка: coupon id/code должен физически присутствовать в меню ресторана
        if coupon_id not in menu_id_set and code not in menu_id_set:
            print(f"  [COUPON SKIP] {mi.get('name', '?')!r}: id={coupon_id} code={code!r} absent from restaurant menu")
            continue
        cname = mi["name"]
        fallback_price = mi["price"] / 100
        code = entry.get("code", "")
        struct_entry = struct["coupons"].get(code) or struct["combos"].get(str(mi["id"]))
        if not struct_entry:
            continue
        slots = struct_entry.get("slots", [])
        if len(slots) != 1:
            continue
        dishes = slots[0].get("dishes", [])
        for d in dishes:
            did = d.get("dish_id")
            if did and did in menu_by_id:
                # Цена купона — своя для каждой позиции (скрытая таблица наценок БК),
                # а не плоская база mi["price"]. Для «Всё по 99,99» Воппер = 249.98, Кола = 99.99.
                dish_price = d.get("price", mi["price"]) / 100
                mono_coupons.append((did, cname, dish_price))

    discount_map = {}
    for did, cname, cprice in mono_coupons:
        old = discount_map.get(did)
        if old is None or cprice < old[1]:
            discount_map[did] = (cname, cprice)

    # Апгрейд: если купон даёт больше того же блюда за меньшие деньги
    for item_name in list(order.keys()):
        e = dish_idx.get(item_name.lower())
        if not e:
            continue
        item_ftype = _food_type(item_name.lower())
        item_qty = _extract_quantity(item_name)
        if not item_ftype:
            continue
        for did, cname, cprice in mono_coupons:
            if did == e["id"]:
                continue
            coupon_dish = menu_by_id.get(did)
            if not coupon_dish:
                continue
            coupon_name = coupon_dish["name"]
            coupon_ftype = _food_type(coupon_name.lower())
            coupon_qty = _extract_quantity(coupon_name)
            if (coupon_ftype == item_ftype
                    and coupon_qty and item_qty and coupon_qty > item_qty
                    and cprice < e["price"]):
                if e["id"] not in discount_map or cprice < discount_map[e["id"]][1]:
                    discount_map[e["id"]] = (cname, cprice)
                    print(f"  [UPGRADE] {item_name} -> {coupon_name} @ {cprice:.2f} (купон)")

    print(f"  Моно-купонов привязано: {sum(1 for _ in mono_coupons)}, без привязки: 0 (отсеяно фантомных: {coupon_skipped})")
    print(f"  Комбо-купонов (2+ слота): {len(menu['combos'])}")

    # ── Подготовка ComboCoupon: нормализация через BKMenuItem ──
    wanted_names = set(order.keys())
    all_combos = []

    for entry in menu["combos"]:
        mi = entry["main_info"]
        cid = mi["id"]

        # Строгая сверка: combo_id должен физически присутствовать в меню ресторана
        if cid not in menu_id_set:
            print(f"  [COMBO SKIP] {mi.get('name', '?')!r}: id={cid} absent from restaurant menu")
            continue

        struct_entry = struct["combos"].get(str(cid))
        if not struct_entry:
            continue

        # Нормализация через bk_api.normalize_combo
        normalized = normalize_combo(entry, struct_entry, rid)

        # Дополнительная проверка lifecycle после нормализации (даты, restricted)
        lc = normalized.get("lifecycle", {})
        if not (lc.get("is_active") and lc.get("is_available") and lc.get("is_visible")):
            reason = lc.get("reject_reason", "lifecycle_failed")
            print(f"  [COMBO SKIP] {mi.get('name', '?')!r}: {reason}")
            continue

        # Фильтрация: убираем группы без доступных опций
        clean_groups = []
        skip = False
        for group in normalized["required_modifier_groups"]:
            filtered_options = []
            for opt in group["options"]:
                oid = opt["option_id"]
                try:
                    did_int = int(oid)
                except (ValueError, TypeError):
                    did_int = None
                if did_int and did_int in menu_by_id:
                    if menu_by_id[did_int]["price"] == 0:
                        continue
                    filtered_options.append(opt)
                elif not did_int:
                    filtered_options.append(opt)

            if not filtered_options:
                skip = True
                break

            clean_groups.append({
                **group,
                "options": filtered_options,
            })

        if skip:
            continue

        # Определяем side_slot_index и sauce_slot_indices
        side_slot_index = None
        sauce_slot_indices = set()
        for gi, group in enumerate(clean_groups):
            if group.get("is_sauce_slot"):
                sauce_slot_indices.add(gi)
                continue
            has_potato = any(
                any(w in opt["name"].lower() for w in POTATO_WORDS)
                for opt in group["options"]
            )
            if has_potato and side_slot_index is None:
                side_slot_index = gi

        base_price = normalized["pricing"]["base_price_kopecks"]

        all_combos.append({
            "id": cid,
            "name": normalized["name"],
            "base_price_kopecks": base_price,
            "base_price_rub": base_price / 100,
            "groups": clean_groups,
            "side_slot_index": side_slot_index,
            "sauce_slot_indices": sauce_slot_indices,
            "lifecycle": normalized["lifecycle"],
        })

    # ── Шаг 1: MonoCoupon как price floor ──
    effective_prices = dict(menu_prices)
    mono_plan = {}
    for item_name in order:
        e = dish_idx.get(item_name.lower())
        if e and e["id"] in discount_map:
            cname, cprice = discount_map[e["id"]]
            if cprice < effective_prices.get(item_name.lower(), 999):
                effective_prices[item_name.lower()] = cprice
                mono_plan[item_name] = (cname, cprice)

    indiv_eff = sum(effective_prices.get(n.lower(), 0) * c for n, c in order.items())

    # ── Шаг 2: перебор ComboCoupon ──
    relevant = []
    for combo in all_combos:
        group_maps = []
        ok = True
        for gi, group in enumerate(combo["groups"]):
            if gi in combo["sauce_slot_indices"]:
                group_maps.append({})
                continue
            option_names = [opt["name"] for opt in group["options"]]
            mapping = {}
            for wn in wanted_names:
                m = _fuzzy_match(wn, option_names)
                if m:
                    for oi, opt in enumerate(group["options"]):
                        if opt["name"] == m:
                            mapping[wn] = oi
                            break
            group_maps.append(mapping)
            if not mapping:
                ok = False
        if ok:
            relevant.append((combo, group_maps))

    print(f"  Релевантных комбо: {len(relevant)}/{len(all_combos)}")

    best_total = indiv_eff
    best_state = None

    # Вариант A: только MonoCoupon
    remaining_mono = order.copy()
    mono_items_used = set()
    for item_name in list(remaining_mono.keys()):
        if item_name in mono_plan:
            remaining_mono[item_name] = 0
            mono_items_used.add(item_name)
    best_state = (mono_items_used, [], remaining_mono, [], {
        "total": indiv_eff, "with_sauce": indiv_eff, "without_sauce": indiv_eff,
    })

    # Вариант B: ComboCoupon ± MonoCoupon
    for r in range(min(len(relevant), 3)):
        for subset in combinations(relevant, r + 1):
            remaining = Counter(order)
            total = 0.0
            total_with_sauce = 0.0
            total_without_sauce = 0.0
            detail = []
            possible = True

            for combo, group_maps in subset:
                chosen_indices = {}
                items_detail = []
                sauce_idxs = combo["sauce_slot_indices"]

                for gi, group in enumerate(combo["groups"]):
                    if gi in sauce_idxs:
                        is_mandatory = group.get("is_required", True) or group.get("min_select", 0) > 0
                        if is_mandatory:
                            cheapest_oi = min(
                                range(len(group["options"])),
                                key=lambda i: group["options"][i]["price_delta_kopecks"],
                            )
                            chosen_indices[gi] = cheapest_oi
                            items_detail.append(("(соус)", group["options"][cheapest_oi]["name"]))
                        continue

                    gmap = group_maps[gi]
                    best = None
                    best_saving = -1e9
                    for wi, oi in gmap.items():
                        if remaining.get(wi, 0) > 0:
                            mp = menu_prices.get(wi.lower(), 0)
                            opt = group["options"][oi]
                            saving = mp - (combo["base_price_kopecks"] + opt["price_delta_kopecks"]) / 100
                            if saving > best_saving:
                                best_saving = saving
                                best = (wi, oi, opt["name"])
                    if best:
                        wi, oi, opt_name = best
                        remaining[wi] -= 1
                        chosen_indices[gi] = oi
                        items_detail.append((wi, opt_name))
                    else:
                        possible = False
                        break

                if not possible:
                    break

                effective_kopecks = calculate_selected_combo_price(combo, chosen_indices)
                effective_rub = effective_kopecks / 100

                sauce_cost = 0.0
                for gi in sauce_idxs:
                    if gi in chosen_indices:
                        opt = combo["groups"][gi]["options"][chosen_indices[gi]]
                        sauce_cost += opt["price_delta_kopecks"] / 100

                total += effective_rub
                total_with_sauce += effective_rub + sauce_cost
                total_without_sauce += effective_rub
                detail.append({
                    "name": combo["name"],
                    "items": items_detail,
                    "effective_price": effective_rub,
                    "sauce_cost": sauce_cost,
                })

            if not possible:
                continue

            # MonoCoupon на остаток
            total_rem = 0.0
            mono_used = set()
            for item_name in list(remaining.keys()):
                if remaining[item_name] > 0:
                    if item_name in mono_plan:
                        total_rem += mono_plan[item_name][1]
                        mono_used.add(item_name)
                        remaining[item_name] -= 1
                    else:
                        total_rem += effective_prices.get(item_name.lower(), 0) * remaining[item_name]
                        remaining[item_name] = 0

            if all(remaining.get(n, 0) <= 0 for n in order):
                candidate = total + total_rem
                if candidate < best_total:
                    best_total = candidate
                    best_state = (mono_used, subset, remaining, detail, {
                        "total": total + total_rem,
                        "combo_total": total,
                        "with_sauce": total_with_sauce + total_rem,
                        "without_sauce": total_without_sauce + total_rem,
                    })

    # ── вывод ──
    mono_used_items, best_combo, final_remaining, detail_combos, best_costs = best_state

    print("=" * 70)
    print("ОПТИМАЛЬНЫЙ ПЛАН")
    print("=" * 70)

    if mono_used_items:
        mono_cost = sum(mono_plan[i][1] for i in mono_used_items)
        print(f"\nМоно-купон ({mono_cost:.2f} руб):")
        for item in mono_used_items:
            cn, cp = mono_plan[item]
            print(f"  + {cn} → {item} @ {cp:.2f}")

    if best_combo:
        combo_total = best_costs["combo_total"]
        print(f"\nКомбо ({combo_total:.2f} руб):")
        for da in detail_combos:
            print(f"  + {da['name']} @ {da['effective_price']:.2f}:")
            for wanted, actual in da["items"]:
                if wanted != actual:
                    print(f"      {wanted} -> {actual}")
                else:
                    print(f"      {actual}")

    if final_remaining:
        leftover = {k: v for k, v in final_remaining.items() if v > 0}
        if leftover:
            extra_cost = sum(effective_prices.get(i.lower(), 0) * c for i, c in leftover.items())
            print(f"\nОтдельно ({extra_cost:.2f} руб):")
            for item, cnt in leftover.items():
                p = effective_prices.get(item.lower(), 0)
                if cnt > 0:
                    print(f"  + {item} x{cnt} = {p*cnt:.2f}")

    # ── Соусный сценарий ──
    w_sauce = best_costs.get("with_sauce", best_total)
    wo_sauce = best_costs.get("without_sauce", best_total)
    if w_sauce != wo_sauce:
        print(f"\n  Соусный слот: без соуса -> {wo_sauce:.2f} руб / с соусом -> {w_sauce:.2f} руб")

    print(f"\nИТОГО: {best_total:.2f} руб")
    if w_sauce != wo_sauce and w_sauce == best_total:
        print(f"  (без соуса: {wo_sauce:.2f} руб)")
    print(f"Без акций: {indiv_total:.2f} руб")
    savings = indiv_total - best_total
    if savings > 0:
        print(f"ЭКОНОМИЯ: {savings:.2f} руб")

    return best_total, savings, best_state, indiv_total, menu_prices, effective_prices, mono_plan, dish_idx, order

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Burger King price optimizer")
    parser.add_argument("items", nargs="*", default=["Воппер", "Кола", "Наггетсы"],
                        help="Menu items to order")
    parser.add_argument("--store", default="1002",
                        help="Restaurant ID (default: 1002)")
    parser.add_argument("--mode", choices=["live", "offline", "auto"], default="auto",
                        help="Data source: live/offline/auto (default: auto)")
    args = parser.parse_args()

    wanted = parse_args(args.items)
    optimize(wanted, restaurant_id=args.store, mode=args.mode)
