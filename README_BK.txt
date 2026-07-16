Файлы проекта Burger King Optimizer:

1. bk_all_menus_combined.json (886 ресторанов, ~175-185 блюд каждый)
   - Полное меню: названия блюд, цены в копейках, комбо, купоны
   - Источник: POST /menu-composition/api/v7/menu

2. optimizer.py — основной инструмент
   Запуск: python optimizer.py [список товаров]
   Пример: python optimizer.py Воппер Кола Наггетсы
   Показ меню: python optimizer.py show

3. capture_browser_api.py — сбор точных структур комбо/купонов
   Запуск: python capture_browser_api.py
   Открывает браузер, перехватывает menu/combo и menu/general_coupon
   Паузы 3-7 сек между запросами, чтобы избежать бана
   Сохраняет в combo_structures.json

4. bk_api_dump/ — дампы API от Playwright (1 combo + 1 coupon)

5. manual_compositions.json — ручная карта составов (шаблон, требует
   сверки с реальными названиями блюд из конкретного ресторана)

Как улучшить:
- Когда IP разблокируется: запустить capture_browser_api.py
- После получения combo_structures.json: optimizer автоматически
  подхватит точные составы комбо/купонов из слотов
- Для нового ресторана: достаточно обновить prices_key в menu.json

API эндпоинты:
- POST .../menu-composition/api/v7/menu — общее меню
- POST .../menu-composition/api/v7/menu/combo — состав комбо (слоты)
- POST .../menu-composition/api/v7/menu/general_coupon — состав купона
- GET .../restaurant-composition/api/v7/restaurants/search — поиск ресторанов
