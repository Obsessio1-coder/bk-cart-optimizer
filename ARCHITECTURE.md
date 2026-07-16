# Архитектура парсера меню Burger King (оптимизатор корзины)

> Роль: Ведущий системный архитектор ИИ-систем.
> Цель: отказоустойчивая архитектура БД и структура API-запросов для парсера.
> Целевой ресторан: Югорск, ТЦ Лайнер, **X-Store-ID: 1002**.

## Закрытые уязвимости

| # | Уязвимость | Решение |
|---|-----------|---------|
| 1 | Срез базовой цены комбо вместо динамической калькуляции обязательных модификаторов (`required_modifiers`) | Формула `EFFECTIVE_COMBO_PRICE` (раздел 2.2) |
| 2 | CDN БК отдаёт «призрачные»/скрытые позиции, игнорируются флаги `visibility`/`activity` | Поля `lifecycle` в схеме + жёсткая фильтрация |
| 3 | Ошибка кастомизации: берётся дефолтная `min_price` вместо реального шага сборки | Дельта выбранной опции (`price_delta_kopecks`) |
| 4 | Нет привязки к ресторану: запросы без `X-Store-ID` | `SESSION_HEADERS` + `SESSION_COOKIES` (раздел 2.1) |

---

## 1. JSON-схема позиции (`menu_item_schema`)

Принципы: цена **всегда в копейках** (int, без float), флаги жизненного цикла явные,
модификаторы — нормализованный массив с дефолтами и тегами для матрицы UPSELL.

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "BKMenuItem",
  "type": "object",
  "required": ["id", "code", "name", "restaurant_id", "type", "lifecycle", "pricing"],
  "properties": {
    "id":            { "type": "string" },
    "code":          { "type": "string" },
    "name":          { "type": "string" },
    "restaurant_id": { "type": "integer", "examples": [1002] },
    "type":          { "type": "string", "enum": ["product", "combo"] },

    "lifecycle": {
      "type": "object",
      "description": "Флаги активности — устраняют уязвимость #2 (призрачные позиции)",
      "required": ["is_active", "is_visible", "is_available"],
      "properties": {
        "is_active":    { "type": "boolean", "description": "Есть в актуальном меню ресторана (activity)" },
        "is_visible":   { "type": "boolean", "description": "Не скрыт флагом visibility/CDN" },
        "is_available": { "type": "boolean", "description": "Не out_of_stock (price>0)" },
        "is_archived":  { "type": "boolean", "description": "Архивная позиция — жёстко отбрасывается" },
        "fetched_at":   { "type": "string", "format": "date-time" }
      }
    },

    "pricing": {
      "type": "object",
      "required": ["base_price_kopecks"],
      "properties": {
        "base_price_kopecks": { "type": "integer", "minimum": 0,
          "description": "Цена из menu.result (в копейках). Делится на 100 только при выводе" },
        "min_price_kopecks":  { "type": "integer", "description": "Нижняя граница кастомизации" },
        "max_price_kopecks":  { "type": "integer", "description": "Верхняя граница кастомизации" },
        "currency":           { "const": "RUB" }
      }
    },

    "required_modifier_groups": {
      "type": "array",
      "description": "Обязательные слоты сборки — устраняют уязвимость #1 и #3",
      "items": {
        "type": "object",
        "required": ["group_id", "is_required", "options"],
        "properties": {
          "group_id":   { "type": "string" },
          "name":       { "type": "string" },
          "is_required":{ "type": "boolean", "const": true },
          "min_select": { "type": "integer", "const": 1 },
          "max_select": { "type": "integer" },
          "is_sauce_slot": { "type": "boolean", "default": false },
          "options": {
            "type": "array",
            "items": {
              "type": "object",
              "required": ["option_id", "name", "price_delta_kopecks", "is_default"],
              "properties": {
                "option_id":           { "type": "string" },
                "name":                { "type": "string" },
                "price_delta_kopecks": { "type": "integer", "description": "Дельта к base_price (0 для дефолта)" },
                "is_default":          { "type": "boolean" },
                "tag":                 { "type": "string", "description": "small_fries|medium_fries|nuggets_6|cola ... для UPSELL_MATRIX" }
              }
            }
          }
        }
      }
    },

    "region": {
      "type": "object",
      "properties": {
        "store_id":      { "type": "integer" },
        "price_updated_at": { "type": "string", "format": "date-time" },
        "regional_markup_kopecks": { "type": "integer", "default": 0 }
      }
    },

    "source": {
      "type": "object",
      "properties": {
        "endpoint": { "type": "string" },
        "cache_key": { "type": "string" }
      }
    }
  }
}
```

**Правило валидации (в коде):** позиция попадает в индекс только если
`is_active && is_visible && is_available && !is_archived && base_price_kopecks > 0`.
Это закрывает уязвимость #2.

---

## 2. ТЗ для кодинг-агента

### 2.1. Session headers / cookies (закрывает уязвимость #4)

Каждая сессия парсера инициализируется **до** любого запроса меню:

```python
SESSION_HEADERS = {
    "X-Store-ID": "1002",                         # Югорск, ТЦ Лайнер — обязателен
    "X-Region-Id": "1002",
    "Authorization": f"Bearer {BK_TOKEN}",
    "User-Agent": "BurgerKing/Android/10.4.2",
    "X-App-Version": "10.4.2",
    "X-Platform": "android",
    "Accept": "application/json",
    "Accept-Language": "ru-RU",
    "Content-Type": "application/json",
}
SESSION_COOKIES = {
    "session_id": BK_SESSION,
    "store_id": "1002",                           # дублируем в cookie на случай прокси CDN
    "basket_id": str(uuid4()),
}
```

> Региональная наценка (#4) читается **только** из ответа для `X-Store-ID: 1002`.
> Запрос без этого заголовка отдаёт федеральные (неправильные) цены — жёстко запрещён.

### 2.2. Математика пересчёта цены комбо (закрывает #1 и #3)

Никогда не брать `base_price` как итог. Итоговая цена комбо = база + обязательные
модификаторы + upsell замен + соусы:

```
EFFECTIVE_COMBO_PRICE =
    base_price_kopecks                                            # из menu.result.combos[].main_info.price
  + Σ_{g ∈ required_modifier_groups}
        selected(g).price_delta_kopecks                          # дефолт = is_default; при замене — дельта выбранной опции
  + Σ_{g ∈ required_modifier_groups, is_sauce_slot=true}
        (selected_sauce ? sauce.price_delta_kopecks : 0)         # сценарий «с соусом / без»
  + Σ_{replacement}
        UPSELL_MATRIX[default_tag(g)][chosen_tag(g)] * 100       # в копейки; нет в матрице → DEFAULT_UPCHARGE*100
```

Где `selected(g)` для дефолтного наполнителя = опция с `is_default=true`
(для гарнира — cheapest potato, для напитка — cheapest cola). Это устраняет
уязвимость #3 (не брать `min_price` как цену сборки — брать реальную дельту
выбранной опции).

**Контроль целостности:** после расчёта сверить `EFFECTIVE_COMBO_PRICE / 100`
с ценой, которую вернул API для этого комбо. Расхождение > 1 коп. → логировать
и перепарсить структуру (защита от дрейфа матрицы).

### 2.3. Порядок обработки (anti-hallucination для Flash-агента)

1. `build_menu_index` → загрузить меню **для store_id=1002**, отфильтровать по `lifecycle` (схема выше).
2. Для каждого комбо: найти `required_modifier_groups` по `id`, отбросить слоты без доступных опций (пустой слот → комбо отбрасывается).
3. Пометить `is_sauce_slot` там, где ≥2 опций содержат «соус»/«кетчуп».
4. Рассчитать `EFFECTIVE_COMBO_PRICE` по формуле 2.2, считать 2 сценария соуса.
5. К остатку заказа применить `MonoCoupon` (price floor по `dish_id`), только если `dish_id` присутствует в индексе 1002.

**Критические запреты для агента:**
- НЕ использовать `central_prices.json` (федеральные средние) для расчёта — только ответ API store 1002.
- НЕ принимать позиции с `is_visible=false`, `is_archived=true` или `base_price_kopecks=0`.
- НЕ считать `min_price` финальной ценой комбо.
