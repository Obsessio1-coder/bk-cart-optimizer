import sys, re, math, argparse
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

def _build_coupon_item_prices(struct):
    """Build {name_lower: (price_in_rub, coupon_name)} from single-slot coupons in struct.

    struct stores prices in kopecks — convert to rubles (÷100).
    """
    prices = {}
    for code, entry in struct.get("coupons", {}).items():
        slots = entry.get("slots", [])
        if len(slots) != 1:
            continue
        for d in slots[0].get("dishes", []):
            name = (d.get("name") or d.get("menu_name", "")).strip()
            if not name:
                continue
            price_kop = d.get("price", 0)
            if price_kop == 0:
                continue
            key = name.lower()
            cname = entry.get("name", code)
            price_rub = price_kop / 100
            if key not in prices or price_rub < prices[key][0]:
                prices[key] = (price_rub, cname)
    return prices


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

# ── порционные эквиваленты ───────────────────────────────────────────

PORTION_SIZES = {
    "small_fries": 1.0,
    "medium_fries": 1.5,
    "large_fries": 2.2,
    "maxx_fries": 3.0,
    "nuggets_3": 3,
    "nuggets_6": 6,
    "nuggets_9": 9,
    "nuggets_12": 12,
    "cola_small": 0.4,
    "cola_medium": 0.5,
    "cola_large": 0.8,
}

def _portion_class(tag):
    for cls in ("fries", "nuggets", "cola"):
        if cls in tag:
            return cls
    return None

def _tag_portion(name):
    from bk_api import tag_side
    tag = tag_side(name)
    if tag:
        return tag
    nl = name.lower()
    if "кола" in nl or "кока" in nl:
        if "мал" in nl:
            return "cola_small"
        if "стандарт" in nl:
            return "cola_medium"
        if "больш" in nl:
            return "cola_large"
        if "0,5" in nl or "0.5" in nl:
            return "cola_medium"
        if "0,8" in nl or "0.8" in nl:
            return "cola_large"
    return None

def _find_smaller_in_class(item_name, dish_idx):
    tag = _tag_portion(item_name)
    if not tag or tag not in PORTION_SIZES:
        return []
    cls = _portion_class(tag)
    if not cls:
        return []
    base_size = PORTION_SIZES[tag]
    smaller = []
    for name_lower, info in dish_idx.items():
        if info.get("combo_only"):
            continue
        other_tag = _tag_portion(info["name"])
        if other_tag and _portion_class(other_tag) == cls:
            other_size = PORTION_SIZES.get(other_tag)
            if other_size and other_size < base_size:
                smaller.append((other_tag, other_size, info["name"]))
    smaller.sort(key=lambda x: x[1], reverse=True)
    return smaller

def generate_alternative_carts(order_dict, dish_idx):
    """Генерирует альтернативные корзины, заменяя порции на бОльшее
    количество меньших порций того же типа (картофель, наггетсы, кола).

    Возвращает список кортежей (Counter(альтернативный_заказ), текст_подсказки).
    """
    alternatives = []
    for item_name, count in list(order_dict.items()):
        if count <= 0:
            continue
        smaller = _find_smaller_in_class(item_name, dish_idx)
        if not smaller:
            continue
        tag = _tag_portion(item_name)
        base_size = PORTION_SIZES[tag]
        needed_volume = base_size * count
        for smaller_tag, smaller_size, smaller_name in smaller:
            alt_count = math.ceil(needed_volume / smaller_size)
            alt_cart = Counter(order_dict)
            alt_cart[item_name] = 0
            alt_cart[smaller_name] += alt_count
            alt_cart = +alt_cart
            extra_pct = (smaller_size * alt_count - needed_volume) / needed_volume * 100 if needed_volume > 0 else 0
            tip = f"Мы заменили {count}x \"{item_name}\" на {alt_count}x \"{smaller_name}\"."
            if extra_pct > 0:
                tip += f" Вы получите на {extra_pct:.0f}% больше!"
            alternatives.append((alt_cart, tip))
    return alternatives

def _apply_multi_mono(remaining, multi_mono_coupons, dish_idx):
    """Применяет мульти-слот купоны с одинаковыми слотами к остатку remaining (in-place).

    Возвращает (added_cost_rub, multi_used), где multi_used — список
    [{"name": ..., "items": {item_name: count}, "price": ...}, ...].
    """
    added = 0.0
    multi_used = []
    for mc in multi_mono_coupons:
        needed = mc["slots_count"]
        avail = []
        for item_name in list(remaining.keys()):
            cnt = remaining[item_name]
            if cnt > 0:
                e = dish_idx.get(item_name.lower())
                if e and e["id"] in mc["common_ids"]:
                    avail.extend([item_name] * cnt)
        if len(avail) >= needed:
            used_items = {}
            for i in range(needed):
                nm = avail[i]
                remaining[nm] -= 1
                used_items[nm] = used_items.get(nm, 0) + 1
                if remaining[nm] == 0:
                    del remaining[nm]
            price_rub = mc["price_kopecks"] / 100
            added += price_rub
            multi_used.append({
                "name": mc["name"],
                "items": used_items,
                "price": price_rub,
            })
    return added, multi_used


def _apply_diverse_multi_mono(remaining, diverse_coupons, dish_idx):
    """Применяет мульти-слот купоны с РАЗНЫМИ типами слотов (напр. бургер+гарнир+соус).

    Каждый слот имеет свой набор dish_ids.
    Нужно подобрать по одному товару из остатка на каждый слот.

    Возвращает (added_cost_rub, multi_used).
    """
    added = 0.0
    multi_used = []
    for dc in diverse_coupons:
        slot_sets = dc["slot_ids"]
        n_slots = len(slot_sets)
        assignment = [None] * n_slots
        used_items = {}

        avail_items = []
        for item_name in list(remaining.keys()):
            cnt = remaining[item_name]
            if cnt > 0:
                e = dish_idx.get(item_name.lower())
                if e:
                    avail_items.extend([(item_name, e["id"])] * cnt)

        for slot_i, s_set in enumerate(slot_sets):
            for idx, (nm, nid) in enumerate(avail_items):
                if nm in used_items:
                    continue
                if nid in s_set:
                    assignment[slot_i] = nm
                    used_items[nm] = used_items.get(nm, 0) + 1
                    break

        if all(a is not None for a in assignment):
            for nm, cnt in used_items.items():
                remaining[nm] -= cnt
                if remaining[nm] == 0:
                    del remaining[nm]
            price_rub = dc["price_kopecks"] / 100
            added += price_rub
            multi_used.append({
                "name": dc["name"],
                "items": dict(used_items),
                "price": price_rub,
            })
    return added, multi_used

# ── внутренний оптимизатор для одной корзины ────────────────────────

def _optimize_cart(order, menu, struct, rid, dish_idx, menu_by_id, menu_id_set, menu_prices, coupon_only_prices=None):
    """Запускает моно-купон + комбо-поиск для переданной корзины order.

    order:           Counter вида {matched_name: count}
    menu_prices:     {name_lower: price_in_rub}

    Возвращает (best_total, best_state, effective_prices, mono_plan, indiv_eff).
    """
    mono_coupons = []
    coupon_ctx = default_coupon_context(rid)
    coupon_skipped = 0
    for entry in menu.get("general_coupons", []):
        ok, reason = validate_coupon_visibility(entry, coupon_ctx)
        if not ok:
            coupon_skipped += 1
            continue
        mi = entry["main_info"]
        coupon_id = mi["id"]
        code = entry.get("code", "")
        if coupon_id not in menu_id_set and code not in menu_id_set:
            continue
        cname = mi["name"]
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
                dish_price = d.get("price", mi["price"]) / 100
                mono_coupons.append((did, cname, dish_price))

    discount_map = {}
    for did, cname, cprice in mono_coupons:
        old = discount_map.get(did)
        if old is None or cprice < old[1]:
            discount_map[did] = (cname, cprice)

    # ── Мульти-слот купоны (≥2 одинаковых слота, напр. «2 соуса на выбор») ──
    multi_mono_coupons = []
    for entry in menu.get("general_coupons", []):
        ok, reason = validate_coupon_visibility(entry, coupon_ctx)
        if not ok:
            continue
        mi = entry["main_info"]
        coupon_id = mi["id"]
        code = entry.get("code", "")
        if coupon_id not in menu_id_set and code not in menu_id_set:
            continue
        struct_entry = struct["coupons"].get(code) or struct["combos"].get(str(mi["id"]))
        if not struct_entry:
            continue
        slots = struct_entry.get("slots", [])
        if len(slots) < 2:
            continue
        dish_id_sets = [set(d.get("dish_id") for d in s.get("dishes", []) if d.get("dish_id")) for s in slots]
        common = dish_id_sets[0]
        for s_set in dish_id_sets[1:]:
            common = common & s_set
        if not common:
            continue
        multi_mono_coupons.append({
            "name": mi["name"],
            "price_kopecks": mi["price"],
            "slots_count": len(slots),
            "common_ids": common,
        })
    multi_mono_coupons.sort(key=lambda x: (-x["slots_count"], x["price_kopecks"]))

    # ── Разнотипные мульти-слот купоны (разные типы блюд в слотах) ──
    diverse_multi_coupons = []
    for entry in menu.get("general_coupons", []):
        ok, reason = validate_coupon_visibility(entry, coupon_ctx)
        if not ok:
            continue
        mi = entry["main_info"]
        coupon_id = mi["id"]
        code = entry.get("code", "")
        if coupon_id not in menu_id_set and code not in menu_id_set:
            continue
        struct_entry = struct["coupons"].get(code) or struct["combos"].get(str(mi["id"]))
        if not struct_entry:
            continue
        slots = struct_entry.get("slots", [])
        if len(slots) < 2:
            continue
        dish_id_sets = [set(d.get("dish_id") for d in s.get("dishes", []) if d.get("dish_id")) for s in slots]
        common = dish_id_sets[0]
        for s_set in dish_id_sets[1:]:
            common = common & s_set
        if common:
            continue  # already handled by multi_mono_coupons
        diverse_multi_coupons.append({
            "name": mi["name"],
            "price_kopecks": mi["price"],
            "slot_ids": dish_id_sets,
        })
    diverse_multi_coupons.sort(key=lambda x: x["price_kopecks"])

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

    effective_prices = dict(menu_prices)
    mono_plan = {}
    for item_name in order:
        e = dish_idx.get(item_name.lower())
        if e and e["id"] in discount_map:
            cname, cprice = discount_map[e["id"]]
            if cprice < effective_prices.get(item_name.lower(), 999):
                effective_prices[item_name.lower()] = cprice
                mono_plan[item_name] = (cname, cprice)

    if coupon_only_prices:
        for name_lower, (cp, cn) in coupon_only_prices.items():
            for orig_name in order:
                if orig_name.lower() == name_lower:
                    mono_plan[orig_name] = (cn, cp)
                    effective_prices[name_lower] = cp
                    break

    indiv_eff = sum(effective_prices.get(n.lower(), 0) * c for n, c in order.items())

    wanted_names = set(order.keys())
    all_combos = []

    for entry in menu["combos"]:
        mi = entry["main_info"]
        cid = mi["id"]
        if cid not in menu_id_set:
            continue
        struct_entry = struct["combos"].get(str(cid))
        if not struct_entry:
            continue
        normalized = normalize_combo(entry, struct_entry, rid, menu_by_id=menu_by_id)
        lc = normalized.get("lifecycle", {})
        if not (lc.get("is_active") and lc.get("is_available") and lc.get("is_visible")):
            continue
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
            clean_groups.append({**group, "options": filtered_options})
        if skip:
            continue
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

    best_total = indiv_eff
    best_state = None

    remaining_mono = order.copy()
    mono_items_used = {}
    for item_name in list(remaining_mono.keys()):
        if item_name in mono_plan:
            remaining_mono[item_name] = 0
            mono_items_used[item_name] = order[item_name]
    remaining_multi = Counter({k: v for k, v in remaining_mono.items() if v > 0})
    multi_added, multi_used_initial = _apply_multi_mono(remaining_multi, multi_mono_coupons, dish_idx)
    diverse_added, diverse_used = _apply_diverse_multi_mono(remaining_multi, diverse_multi_coupons, dish_idx)
    multi_added += diverse_added
    multi_used_initial.extend(diverse_used)
    initial_total = sum(mono_plan[i][1] * cnt for i, cnt in mono_items_used.items()) if mono_items_used else 0.0
    initial_total += multi_added
    for item_name, cnt in remaining_multi.items():
        if cnt > 0:
            initial_total += effective_prices.get(item_name.lower(), 0) * cnt
    initial_leftover = {k: v for k, v in remaining_multi.items() if v > 0}
    best_state = (mono_items_used, [], remaining_multi, [], {
        "total": initial_total,
        "with_sauce": initial_total,
        "without_sauce": initial_total,
        "saving_tip": "",
        "leftover": initial_leftover,
        "multi_used": multi_used_initial,
    })
    if initial_total < best_total:
        best_total = initial_total

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

            total_rem = 0.0
            mono_used = {}

            # Сначала мульти-слот купоны (напр. 2 соуса за 99,99)
            multi_added, multi_used = _apply_multi_mono(remaining, multi_mono_coupons, dish_idx)
            div_added, div_used = _apply_diverse_multi_mono(remaining, diverse_multi_coupons, dish_idx)
            multi_added += div_added
            multi_used.extend(div_used)
            total_rem += multi_added

            # Потом однослотовые моно-купон и остаток
            leftover = {}
            for item_name in list(remaining.keys()):
                if remaining[item_name] > 0:
                    if item_name in mono_plan:
                        cnt = remaining[item_name]
                        total_rem += mono_plan[item_name][1] * cnt
                        mono_used[item_name] = cnt
                        remaining[item_name] = 0
                    else:
                        cnt = remaining[item_name]
                        total_rem += effective_prices.get(item_name.lower(), 0) * cnt
                        leftover[item_name] = cnt
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
                        "saving_tip": "",
                        "leftover": leftover,
                        "multi_used": multi_used,
                    })

    return best_total, best_state, effective_prices, mono_plan, indiv_eff

# ── главная функция ───────────────────────────────────────────────────

def optimize(wanted_list, restaurant_id="1002", mode="auto"):
    menu, struct, rid = load_data(rid=restaurant_id, mode=mode)
    dish_idx, menu_by_id, removed_menu, reasons = build_menu_index(menu, rid)
    combo_prices, combo_skipped = build_combo_prices(menu, rid)
    menu_id_set = build_menu_id_set(menu)

    # ── Coupon-only item prices from struct ──
    coupon_item_prices = _build_coupon_item_prices(struct)

    # Матчинг заказа
    matched_names = []
    coupon_only_added = {}
    for item in wanted_list:
        name = match_item(item, dish_idx)
        if name:
            matched_names.append(name)
        elif item.lower() in coupon_item_prices:
            cp, cn = coupon_item_prices[item.lower()]
            print(f"  [COUPON ONLY] {item} -> {cn} @ {cp:.2f}")
            matched_names.append(item)
            coupon_only_added[item.lower()] = (cp, cn)
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
        elif n.lower() in coupon_only_added:
            menu_prices[n.lower()] = coupon_only_added[n.lower()][0]

    indiv_total = sum(menu_prices.get(n.lower(), 0) * c for n, c in order.items())

    print(f"\nРесторан {rid}")
    print(f"Отсеяно из меню: {removed_menu} (price_zero={reasons['price_zero']}, restricted={reasons['restricted']})")
    print(f"Комбо/купоны отброшены по lifecycle: {combo_skipped}")
    print("Заказ:")
    for n, c in sorted(order.items()):
        p = menu_prices.get(n.lower(), 0)
        co = " [только в комбо]" if dish_idx.get(n.lower(), {}).get("combo_only") else ""
        co2 = " [купон]" if n.lower() in coupon_only_added else ""
        print(f"  {n} x{c} @ {p:.2f}{co}{co2}")
    print(f"  Без скидок: {indiv_total:.2f} руб")

    # ── Генерация альтернативных корзин ──
    alt_carts = generate_alternative_carts(order, dish_idx)
    if alt_carts:
        print(f"\n  Alts ({len(alt_carts)}):")
        for alt_order, tip in alt_carts:
            alt_summary = ", ".join(f"{k} x{v}" for k, v in sorted(alt_order.items()) if v > 0)
            print(f"    {tip}  [{alt_summary}]")

    # ── Оптимизация для всех корзин ──
    best_total, best_state, best_eff, best_mono, _ = _optimize_cart(
        order, menu, struct, rid, dish_idx, menu_by_id, menu_id_set, menu_prices,
        coupon_only_prices=coupon_only_added,
    )
    best_alt_tip = ""
    used_alternative = False

    for alt_order, alt_tip in alt_carts:
        alt_menu_prices = {}
        for n in alt_order:
            e = dish_idx.get(n.lower())
            if e and e["price"] > 0 and not e.get("combo_only"):
                alt_menu_prices[n.lower()] = e["price"]
        alt_total, alt_state, alt_eff, alt_mono, _ = _optimize_cart(
            alt_order, menu, struct, rid, dish_idx, menu_by_id, menu_id_set, alt_menu_prices,
        )
        if alt_total < best_total:
            best_total = alt_total
            best_state = alt_state
            best_eff = alt_eff
            best_mono = alt_mono
            best_alt_tip = alt_tip
            used_alternative = True

    savings = indiv_total - best_total

    # ── вывод ──
    mono_used_items, best_combo, final_remaining, detail_combos, best_costs = best_state

    best_costs["saving_tip"] = best_alt_tip

    print()
    print("=" * 70)
    print("ОПТИМАЛЬНЫЙ ПЛАН")
    print("=" * 70)

    if best_alt_tip:
        print(f"\n💡 {best_alt_tip}")

    if mono_used_items:
        mono_cost = sum(best_mono[i][1] * cnt for i, cnt in mono_used_items.items())
        print(f"\nМоно-купон ({mono_cost:.2f} руб):")
        for item, cnt in mono_used_items.items():
            cn, cp = best_mono[item]
            print(f"  + {cn} → {item} x{cnt} @ {cp:.2f}")

    multi_used = best_costs.get("multi_used", [])
    if multi_used:
        multi_cost = sum(m["price"] for m in multi_used)
        print(f"\nМульти-купон ({multi_cost:.2f} руб):")
        for m in multi_used:
            print(f"  + {m['name']} @ {m['price']:.2f}:")
            for iname, icnt in m["items"].items():
                print(f"      {iname}" + (f" x{icnt}" if icnt > 1 else ""))

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

    leftover = best_costs.get("leftover", {})
    if leftover:
        extra_cost = sum(best_eff.get(i.lower(), 0) * c for i, c in leftover.items())
        print(f"\nОтдельно ({extra_cost:.2f} руб):")
        for item, cnt in leftover.items():
            p = best_eff.get(item.lower(), 0)
            if cnt > 0:
                print(f"  + {item} x{cnt} = {p*cnt:.2f}")

    w_sauce = best_costs.get("with_sauce", best_total)
    wo_sauce = best_costs.get("without_sauce", best_total)
    if w_sauce != wo_sauce:
        print(f"\n  Соусный слот: без соуса -> {wo_sauce:.2f} руб / с соусом -> {w_sauce:.2f} руб")

    print(f"\nИТОГО: {best_total:.2f} руб")
    if w_sauce != wo_sauce and w_sauce == best_total:
        print(f"  (без соуса: {wo_sauce:.2f} руб)")
    print(f"Без акций: {indiv_total:.2f} руб")
    if savings > 0:
        print(f"ЭКОНОМИЯ: {savings:.2f} руб")

    return best_total, savings, best_state, indiv_total, menu_prices, best_eff, best_mono, dish_idx, order

    # ── вывод ──
    mono_used_items, best_combo, final_remaining, detail_combos, best_costs = best_state

    print("=" * 70)
    print("ОПТИМАЛЬНЫЙ ПЛАН")
    print("=" * 70)

    if mono_used_items:
        mono_cost = sum(mono_plan[i][1] * cnt for i, cnt in mono_used_items.items())
        print(f"\nМоно-купон ({mono_cost:.2f} руб):")
        for item, cnt in mono_used_items.items():
            cn, cp = mono_plan[item]
            print(f"  + {cn} → {item} x{cnt} @ {cp:.2f}")

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
