import sys, os, re, json
from collections import Counter
from itertools import combinations
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from bk_api import (
    load_data, build_menu_index, build_combo_prices, build_menu_id_set,
    normalize_combo, validate_coupon_visibility, default_coupon_context,
    tag_sauce,
    POTATO_WORDS, UPSELL_MATRIX, DEFAULT_UPCHARGE,
)

from optimizer import (
    _extract_quantity, _food_type, _fuzzy_match, match_item,
    calculate_selected_combo_price,
)

app = Flask(__name__)
CORS(app)


def _prep_mono_coupons(menu, struct, menu_by_id, menu_id_set, restaurant_id):
    from optimizer import _food_type, _extract_quantity

    mono_coupons = []
    coupon_ctx = default_coupon_context(restaurant_id)
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
        fallback_price = mi["price"]
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
                dish_price = d.get("price", fallback_price) / 100
                mono_coupons.append((did, cname, dish_price))

    discount_map = {}
    for did, cname, cprice in mono_coupons:
        old = discount_map.get(did)
        if old is None or cprice < old[1]:
            discount_map[did] = (cname, cprice)

    return discount_map, mono_coupons, coupon_skipped


def optimize_api(wanted_list, restaurant_id="1002", mode="auto"):
    menu, struct, rid = load_data(rid=restaurant_id, mode=mode)
    dish_idx, menu_by_id, removed_menu, reasons = build_menu_index(menu, rid)
    combo_prices, combo_skipped = build_combo_prices(menu, rid)
    menu_id_set = build_menu_id_set(menu)

    matched_names = []
    not_found = []
    for item in wanted_list:
        name = match_item(item, dish_idx)
        if name:
            matched_names.append(name)
        else:
            not_found.append(item)
    if not matched_names:
        return {"error": "Ничего не найдено", "not_found": not_found}

    order = Counter(matched_names)
    menu_prices = {}
    for n in order:
        e = dish_idx.get(n.lower())
        if e and e["price"] > 0 and not e.get("combo_only"):
            menu_prices[n.lower()] = e["price"]

    indiv_total = sum(menu_prices.get(n.lower(), 0) * c for n, c in order.items())

    # Mono coupons
    discount_map, mono_coupons, coupon_skipped = _prep_mono_coupons(
        menu, struct, menu_by_id, menu_id_set, restaurant_id
    )

    # Upgrade: same food type, more quantity, cheaper price
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

    # Effective prices with mono coupons
    effective_prices = dict(menu_prices)
    mono_plan = {}
    for item_name in order:
        e = dish_idx.get(item_name.lower())
        if e and e["id"] in discount_map:
            cname, cprice = discount_map[e["id"]]
            if cprice < effective_prices.get(item_name.lower(), 999999):
                effective_prices[item_name.lower()] = cprice
                mono_plan[item_name] = (cname, cprice)

    indiv_eff = sum(effective_prices.get(n.lower(), 0) * c for n, c in order.items())

    # Normalize combos
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
            "base_price_rub": round(base_price / 100, 2),
            "groups": clean_groups,
            "side_slot_index": side_slot_index,
            "sauce_slot_indices": sauce_slot_indices,
            "lifecycle": normalized["lifecycle"],
        })

    # Match combos to order
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

    # Search best combination
    best_total = indiv_eff
    best_state = None

    remaining_mono = order.copy()
    mono_items_used = set()
    for item_name in list(remaining_mono.keys()):
        if item_name in mono_plan:
            remaining_mono[item_name] = 0
            mono_items_used.add(item_name)

    initial_remaining = {}
    for item_name, cnt in remaining_mono.items():
        if cnt > 0:
            price = effective_prices.get(item_name.lower(), 0)
            combo_only = item_name.lower() not in menu_prices
            initial_remaining[item_name] = {"count": cnt, "price": price, "combo_only": combo_only}
    best_state = {
        "mono_items_used": mono_items_used,
        "combos": [],
        "remaining": initial_remaining,
        "total": indiv_eff,
        "combo_total": 0,
        "savings": round(indiv_total - indiv_eff, 2),
    }

    for r in range(min(len(relevant), 3)):
        for subset in combinations(relevant, r + 1):
            remaining = Counter(order)
            total = 0.0
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
                            items_detail.append({
                                "wanted": None,
                                "selected": group["options"][cheapest_oi]["name"],
                                "is_sauce": True,
                            })
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
                        items_detail.append({
                            "wanted": wi,
                            "selected": opt_name,
                        })
                    else:
                        possible = False
                        break

                if not possible:
                    break

                effective_kopecks = calculate_selected_combo_price(
                    {"base_price_kopecks": combo["base_price_kopecks"], "groups": combo["groups"]},
                    chosen_indices
                )
                effective_rub = effective_kopecks / 100

                detail.append({
                    "name": combo["name"],
                    "items": items_detail,
                    "effective_price": round(effective_rub, 2),
                })
                total += effective_rub

            if not possible:
                continue

            total_rem = 0.0
            mono_used = set()
            remaining_detail = {}
            for item_name in list(remaining.keys()):
                if remaining[item_name] > 0:
                    if item_name in mono_plan:
                        cnt = remaining[item_name]
                        total_rem += mono_plan[item_name][1] * cnt
                        mono_used.add(item_name)
                        remaining_detail[item_name] = {
                            "count": cnt,
                            "price": mono_plan[item_name][1],
                            "coupon": mono_plan[item_name][0],
                            "combo_only": False,
                        }
                        remaining[item_name] = 0
                    else:
                        price = effective_prices.get(item_name.lower(), 0)
                        cnt = remaining[item_name]
                        combo_only = item_name.lower() not in menu_prices
                        remaining_detail[item_name] = {
                            "count": cnt,
                            "price": price,
                            "combo_only": combo_only,
                        }
                        remaining[item_name] = 0

            if all(remaining.get(n, 0) <= 0 for n in order):
                candidate = total + total_rem
                if candidate < best_total:
                    best_total = candidate
                    best_state = {
                        "mono_items_used": mono_used,
                        "combos": detail,
                        "remaining": remaining_detail,
                        "total": round(total + total_rem, 2),
                        "combo_total": round(total, 2),
                        "savings": round(indiv_total - (total + total_rem), 2),
                    }

    # Build mono coupon details for the response
    mono_details = {}
    for item_name in best_state["mono_items_used"]:
        if item_name in mono_plan:
            cn, cp = mono_plan[item_name]
            mono_details[item_name] = {"coupon_name": cn, "price": round(cp, 2)}

    result = {
        "restaurant_id": rid,
        "order": [{"name": n, "count": c} for n, c in sorted(order.items())],
        "menu_total": round(indiv_total, 2),
        "best_total": best_state["total"],
        "savings": best_state["savings"],
        "plan": {
            "mono_coupons": mono_details,
            "combos": best_state["combos"],
            "remaining": best_state["remaining"],
        },
        "not_found": not_found,
        "stats": {
            "menu_items_total": len(menu.get("dishes", [])),
            "combos_total": len(menu["combos"]),
            "combos_relevant": len(relevant),
            "combos_skipped": combo_skipped,
            "coupons_skipped": coupon_skipped,
        },
    }
    return result


def build_frontend_response(result):
    best_total, savings, best_state, menu_total, menu_prices, effective_prices, mono_plan, dish_idx, order = result
    mono_used_items, best_combo, final_remaining, detail_combos, best_costs = best_state

    menu_total_kop = round(menu_total * 100)
    best_total_kop = round(best_costs["total"] * 100)

    combos_list = []
    for dc in detail_combos:
        items_list = []
        for wanted, selected in dc["items"]:
            items_list.append({
                "wanted": None if wanted == "(соус)" else wanted,
                "selected": selected,
                "is_sauce": wanted == "(соус)",
            })
        combos_list.append({
            "name": dc["name"],
            "items": items_list,
            "effective_price_kopecks": round(dc["effective_price"] * 100),
        })

    mono_coupons = {}
    for item_name, cnt in mono_used_items.items():
        if item_name in mono_plan:
            cn, cp = mono_plan[item_name]
            mono_coupons[item_name] = {"coupon_name": cn, "price_kopecks": round(cp * 100), "count": cnt}

    leftover = best_costs.get("leftover", {})
    remaining = {}
    for item_name, cnt in leftover.items():
        if cnt > 0:
            price_kop = round(menu_prices.get(item_name.lower(), 0) * 100)
            combo_only = item_name.lower() not in menu_prices
            remaining[item_name] = {"count": cnt, "price_kopecks": price_kop, "combo_only": combo_only}

    multi_used = best_costs.get("multi_used", [])
    multi_coupons = []
    for m in multi_used:
        items_list = [{"name": iname, "count": icnt} for iname, icnt in m["items"].items()]
        multi_coupons.append({
            "name": m["name"],
            "items": items_list,
            "price_kopecks": round(m["price"] * 100),
        })

    saving_tip = best_costs.get("saving_tip", "")

    return {
        "menu_total": menu_total_kop,
        "best_total": best_total_kop,
        "savings": round(savings * 100),
        "saving_tip": saving_tip,
        "plan": {
            "combos": combos_list,
            "mono_coupons": mono_coupons,
            "multi_coupons": multi_coupons,
            "remaining": remaining,
        },
    }


@app.route("/optimize", methods=["POST"])
def optimize():
    data = request.get_json(force=True)
    items = data.get("items", [])
    restaurant_id = str(data.get("store", "1002"))
    mode = data.get("mode", "auto")

    if not items:
        return jsonify({"error": "Поле 'items' обязательно и не должно быть пустым"}), 400

    try:
        from optimizer import optimize as opt_run
        result = opt_run(items, restaurant_id=restaurant_id, mode=mode)
    except FileNotFoundError:
        return jsonify({"error": "Меню этого ресторана временно недоступно. Выберите другой ресторан или попробуйте позже."}), 400
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print("[OPTIMIZE ERROR]", tb)
        return jsonify({"error": str(e), "type": type(e).__name__, "trace": tb}), 500

    if result is None:
        return jsonify({"error": "Ни одно блюдо не найдено в меню"}), 400

    return jsonify(build_frontend_response(result))


def _load_restaurants():
    path = os.path.join(BASE_DIR, "bk_all_menus", "__restaurants.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _build_group_map(menu):
    groups_map = {}
    for g in menu.get("groups", []):
        mi = g["main_info"]
        gid = mi["id"]
        gname = mi["name"]
        for did in g.get("included_dishes", []):
            grps = groups_map.setdefault(did, [])
            if gname not in grps:
                grps.append(gname)

    # Remove generic "Эвервесс Кола" for dishes that have a more specific cola group
    cola_specific = {"Эвервесс Кола Вишня", "Эвервесс Кола без сахара"}
    for did, grps in groups_map.items():
        if "Эвервесс Кола" in grps and any(s in grps for s in cola_specific):
            groups_map[did] = [g for g in grps if g != "Эвервесс Кола"]
    return groups_map


@app.route("/restaurants", methods=["GET"])
def get_restaurants():
    search = request.args.get("search", "").strip().lower()

    try:
        all_restaurants = _load_restaurants()
        result = []
        for r in all_restaurants:
            name = r.get("name", "")
            city = r.get("city", {}).get("city_name", "")
            rid = r["id"]
            if search:
                if search not in name.lower() and search not in city.lower():
                    continue
            result.append({"id": rid, "name": name, "city": city})

        result.sort(key=lambda x: (x["city"], x["name"]))
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _load_struct_items():
    """Load items from combo_structures, separated by source.

    Returns:
        coupon_did_names: dish_ids found in struct['coupons']
        combo_only_did_names: dish_ids found ONLY in struct['combos'] (not in coupons)
        coupon_code_by_did: dict mapping dish_id -> set of coupon codes that contain it
    """
    import os
    struct_path = os.path.join(BASE_DIR, "combo_structures.json")
    if not os.path.exists(struct_path):
        return {}, {}, {}
    with open(struct_path, "r", encoding="utf-8") as f:
        struct = json.load(f)

    all_did_names = {}
    coupon_did_names = {}
    coupon_code_by_did = {}

    for code, entry in struct.get("coupons", {}).items():
        code_str = str(code)
        for slot in entry.get("slots", []):
            for d in slot.get("dishes", []):
                did = d.get("dish_id")
                if did:
                    name = d.get("name") or d.get("menu_name")
                    coupon_did_names[did] = name
                    all_did_names[did] = name
                    coupon_code_by_did.setdefault(did, set()).add(code_str)
            for opt in slot.get("options", []):
                did = opt.get("dish_id")
                if did:
                    name = opt.get("name") or opt.get("menu_name")
                    coupon_did_names[did] = name
                    all_did_names[did] = name
                    coupon_code_by_did.setdefault(did, set()).add(code_str)

    combo_only_did_names = {}
    for entry in struct.get("combos", {}).values():
        for slot in entry.get("slots", []):
            for d in slot.get("dishes", []):
                did = d.get("dish_id")
                if did and did not in all_did_names:
                    name = d.get("name") or d.get("menu_name")
                    combo_only_did_names[did] = name
                    all_did_names[did] = name
            for opt in slot.get("options", []):
                did = opt.get("dish_id")
                if did and did not in all_did_names:
                    name = opt.get("name") or opt.get("menu_name")
                    combo_only_did_names[did] = name
                    all_did_names[did] = name

    return coupon_did_names, combo_only_did_names, coupon_code_by_did


_NON_FOOD_KEYWORDS = [
    "салфетк", "перчатки", "сумка", "игрушк", "конструктор",
    "брелок", "носки", "фигурк", "шопер",
]


def _is_non_food(name):
    nl = name.lower()
    return any(k in nl for k in _NON_FOOD_KEYWORDS)


def _should_show_coupon_item(name):
    if _is_non_food(name):
        return False
    # Hide limited-edition flavor variants that aren't standalone items
    nl = name.lower()
    if "жюльен" in nl:
        return False
    if "лобстер" in nl and "соус" not in nl:
        return False
    if "конструктор" in nl:
        return False
    return True


def _categorize_coupon_item(name):
    """Assign relevant groups (categories) to a coupon-only item based on its name."""
    nl = name.lower()
    groups = ["Только в комбо"]

    # Cola variants
    if "эвервесс кола вишня" in nl:
        groups.append("Эвервесс Кола Вишня")
    elif "эвервесс кола б/с" in nl or "эвервесс кола без сахара" in nl:
        groups.append("Эвервесс Кола без сахара")
    elif "эвервесс кола" in nl:
        groups.append("Эвервесс Кола")

    # Other drinks
    if "фрустайл апельсин" in nl:
        groups.append("Фрустайл Апельсин")
    elif "фрустайл лимон" in nl:
        groups.append("Фрустайл Лимон Лайм")
    if "липтон лимон" in nl and "грин" not in nl and "зелен" not in nl:
        groups.append("Липтон")
    if "липтон грин" in nl or "липтон зелен" in nl:
        groups.append("Липтон Грин")
    if "лимонад" in nl:
        groups.append("Напитки")
    if "кофе" in nl:
        groups.append("Кофе")

    # Nuggets & strips
    if "наггетс" in nl:
        groups.append("Наггетсы")
    if "стрипс" in nl:
        groups.append("Наггетсы и стрипсы")

    # Fries & potato
    if "кинг фри" in nl:
        groups.append("Кинг Фри")
    if "картофель деревенский" in nl:
        groups.append("Картофель Деревенский")

    # Burgers
    if "воппер" in nl:
        groups.append("Воппер")
    if "чизбургер" in nl:
        groups.append("Чизбургер")
    if "чикенбургер" in nl:
        groups.append("Чикенбургер")
    if "гамбургер" in nl:
        groups.append("Гамбургер")
    if "фиш бургер" in nl:
        groups.append("Фиш Бургер")

    # Snacks
    if any(k in nl for k in ("креветк", "крылышк", "луковые кольца", "кинг букет", "кинг гоу")):
        groups.append("Закуски")

    # Desserts
    if any(k in nl for k in ("милкшейк", "сандэй", "рожок", "брауни", "маффин", "мороженое")):
        groups.append("Десерты")

    return groups


@app.route("/menu", methods=["GET"])
def get_menu():
    restaurant_id = str(request.args.get("store", "1002"))

    try:
        menu, _, _ = load_data(rid=restaurant_id, mode="auto")
        groups_map = _build_group_map(menu)

        menu_dish_ids = set()
        menu_by_id = {}
        items = []
        for d in menu.get("dishes", []):
            mi = d["main_info"]
            price = mi["price"]
            name = mi["name"].strip()
            did = mi["id"]
            menu_by_id[did] = mi

            if price > 0 and not mi.get("restricted", False):
                menu_dish_ids.add(did)
                grps = groups_map.get(did, [])
                if not grps and tag_sauce(name):
                    grps = ["Соусы"]
                items.append({
                    "name": name,
                    "price_kopecks": price,
                    "groups": grps,
                    "card_type": mi.get("card_type", "standard"),
                })

        # Add items from combo structures (not in regular menu)
        coupon_did_names, combo_only_did_names, coupon_code_by_did = _load_struct_items()
        seen_added_names = set()
        # Prevent struct items with same name from overriding regular items
        for item in items:
            seen_added_names.add(item["name"].lower().strip())

        # Build a set of normalized (size-stripped) names from regular menu items
        _SIZE_QUALIFIERS = (" малый", " стандартный", " большой", " рожок", " классика", " MAXX")
        def _strip_size(name):
            nl = name.lower().strip()
            for q in _SIZE_QUALIFIERS:
                if nl.endswith(q):
                    return nl[:-len(q)].strip()
            return nl
        regular_normalized = {_strip_size(item["name"]) for item in items}

        # Build set of coupon codes that exist at this restaurant
        restaurant_coupon_codes = set()
        # Build set of dish_ids directly referenced in restaurant coupons (mono-coupons)
        restaurant_coupon_dish_ids = set()
        for c in menu.get("general_coupons", []):
            code = c.get("code", "")
            if code:
                restaurant_coupon_codes.add(str(code))
            cid = c.get("dish_id")
            if cid:
                restaurant_coupon_dish_ids.add(cid)

        # Items from coupons -> "только в купонах" (if parent coupon exists at this restaurant
        # AND the dish actually exists in the restaurant's menu by dish_id)
        for did, cname in sorted(coupon_did_names.items(), key=lambda x: x[1]):
            if did in menu_dish_ids:
                continue
            if not _should_show_coupon_item(cname):
                continue
            name_key = cname.lower().strip()
            if name_key in seen_added_names:
                continue
            # If the struct item is a size variant of a regular menu item (same base name),
            # skip it — the regular item already covers it
            if _strip_size(cname) in regular_normalized:
                continue
            seen_added_names.add(name_key)
            parent_coupons = coupon_code_by_did.get(did, set())
            has_parent_coupon = bool(parent_coupons & restaurant_coupon_codes)
            # Item exists in the restaurant menu in ANY state
            in_menu = did in menu_by_id
            # Item is genuinely available (not restricted, not out-of-stock)
            actually_available = in_menu and menu_by_id[did].get("price", 0) > 0 and not menu_by_id[did].get("restricted", False)
            if actually_available:
                # Should have been added as a regular item and skipped above
                continue
            # For items not in the restaurant menu at all, trust the struct
            # For items in the menu but restricted/price=0, treat as unavailable
            dish_available = not in_menu
            is_available = has_parent_coupon and dish_available
            items.append({
                "name": cname,
                "price_kopecks": 0,
                "groups": _categorize_coupon_item(cname),
                "card_type": "coupon_only" if is_available else "absent",
                "coupon_only": True if is_available else False,
                "absent": False if is_available else True,
            })

        # Items only in combo structures (not in menu, not in struct coupons)
        # Check if available via direct mono-coupon in general_coupons
        for did, cname in sorted(combo_only_did_names.items(), key=lambda x: x[1]):
            if did in menu_dish_ids:
                continue
            if not _should_show_coupon_item(cname):
                continue
            name_key = cname.lower().strip()
            if name_key in seen_added_names:
                continue
            seen_added_names.add(name_key)
            is_available_via_coupon = did in restaurant_coupon_dish_ids
            items.append({
                "name": cname,
                "price_kopecks": 0,
                "groups": _categorize_coupon_item(cname),
                "card_type": "coupon_only" if is_available_via_coupon else "absent",
                "absent": not is_available_via_coupon,
                "coupon_only": True if is_available_via_coupon else False,
            })

        items.sort(key=lambda x: x["name"])
        return jsonify(items)

    except FileNotFoundError:
        return jsonify({"error": f"Меню для ресторана {restaurant_id} не найдено. Сервер не может загрузить данные временно."}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/menu/<rid>", methods=["GET"])
def serve_menu_json(rid):
    path = os.path.join(BASE_DIR, "bk_all_menus", f"menu_{rid}.json")
    if not os.path.exists(path):
        return jsonify({"error": f"Меню для ресторана {rid} не найдено"}), 404
    try:
        with open(path, encoding="utf-8") as f:
            m = json.load(f)
        result = m.get("result") or m
        menu = {
            "combos": [
                {"id": c["main_info"]["id"], "name": c["main_info"]["name"], "price": c["main_info"]["price"]}
                for c in result.get("combos", [])
            ],
            "coupons": [
                {"code": c.get("code", ""), "id": c["main_info"]["id"], "name": c["main_info"]["name"], "price": c["main_info"]["price"]}
                for c in result.get("general_coupons", [])
            ],
            "dishes": [
                {"id": d["main_info"]["id"], "name": d["main_info"]["name"], "price": d["main_info"]["price"]}
                for d in result.get("dishes", [])
            ],
        }
        return jsonify(menu)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/")
def serve_frontend():
    return send_from_directory(BASE_DIR, "index.html")


# Vercel Python Serverless entry point
app = app
