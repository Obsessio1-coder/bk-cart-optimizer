import json, random

random.seed(42)
d = json.load(open("bk_all_menus_combined.json", encoding="utf-8"))

rids = list(d.keys())
rid = random.choice(rids)
menu = d[rid]["menu"]["result"]

# 3 separate items
dishes = [x["main_info"] for x in menu["dishes"] if x["main_info"]["price"] > 0]
random.shuffle(dishes)
sel_dishes = dishes[:3]

# 3 coupons
coups = menu.get("general_coupons", [])
random.shuffle(coups)
sel_coups = coups[:3]

# 3 combos
combos = [x["main_info"] for x in menu["combos"]]
random.shuffle(combos)
sel_combos = combos[:3]

with open("menu_sample.txt", "w", encoding="utf-8") as f:
    f.write(f"Ресторан ID: {rid}\n\n")

    for d in sel_dishes:
        f.write(d["name"] + "\n")
        f.write(f"{d['price']/100:.2f} руб\n\n")

    for c in sel_coups:
        f.write(c["main_info"]["name"] + "\n")
        f.write(f"{c['main_info']['price']/100:.2f} руб\n\n")

    for c in sel_combos:
        f.write(c["name"] + "\n")
        f.write(f"{c['price']/100:.2f} руб\n\n")

print("Готово: menu_sample.txt")
