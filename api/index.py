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

        normalized = normalize_combo(entry, struct_entry, rid)
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
                        total_rem += mono_plan[item_name][1]
                        mono_used.add(item_name)
                        remaining_detail[item_name] = {
                            "count": 1,
                            "price": mono_plan[item_name][1],
                            "coupon": mono_plan[item_name][0],
                            "combo_only": False,
                        }
                        remaining[item_name] -= 1
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
    for item_name in mono_used_items:
        if item_name in mono_plan:
            cn, cp = mono_plan[item_name]
            mono_coupons[item_name] = {"coupon_name": cn, "price_kopecks": round(cp * 100)}

    remaining = {}
    for item_name, cnt in final_remaining.items():
        if cnt > 0:
            price_kop = round(menu_prices.get(item_name.lower(), 0) * 100)
            if item_name in mono_plan:
                price_kop = round(mono_plan[item_name][1] * 100)
            remaining[item_name] = {"count": cnt, "price_kopecks": price_kop}

    saving_tip = best_costs.get("saving_tip", "")

    return {
        "menu_total": menu_total_kop,
        "best_total": best_total_kop,
        "savings": round(savings * 100),
        "saving_tip": saving_tip,
        "plan": {
            "combos": combos_list,
            "mono_coupons": mono_coupons,
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
            groups_map.setdefault(did, []).append(gname)
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


@app.route("/menu", methods=["GET"])
def get_menu():
    restaurant_id = str(request.args.get("store", "1002"))

    try:
        menu, _, _ = load_data(rid=restaurant_id, mode="auto")
        groups_map = _build_group_map(menu)

        items = []
        for d in menu.get("dishes", []):
            mi = d["main_info"]
            price = mi["price"]
            name = mi["name"].strip()
            did = mi["id"]

            if price > 0 and not mi.get("restricted", False):
                items.append({
                    "name": name,
                    "price_kopecks": price,
                    "groups": groups_map.get(did, []),
                    "card_type": mi.get("card_type", "standard"),
                })

        items.sort(key=lambda x: x["name"])
        return jsonify(items)

    except FileNotFoundError:
        return jsonify({"error": f"Меню для ресторана {restaurant_id} не найдено. Сервер не может загрузить данные временно."}), 404
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
