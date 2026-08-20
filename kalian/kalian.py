import asyncio
import urllib.parse
import openpyxl
import os
import re
import sys
import random
import time
import json
from playwright.async_api import async_playwright, Error as PlaywrightError
import warnings

warnings.filterwarnings("ignore", message="coroutine 'process_firm' was never awaited")

# --- ИСПРАВЛЕНИЕ ОШИБКИ ПРИ НАЖАТИИ CTRL+C НА WINDOWS ---
if sys.platform.startswith('win'):
    from asyncio.proactor_events import _ProactorBasePipeTransport
    
    def silence_event_loop_closed(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except (RuntimeError, ValueError) as e:
                if str(e) not in ['Event loop is closed', 'I/O operation on closed pipe']:
                    raise
        return wrapper
        
    _ProactorBasePipeTransport.__del__ = silence_event_loop_closed(_ProactorBasePipeTransport.__del__)
# ---------------------------------------------------------

SAVE_EVERY_N = 5

def get_firm_id(url):
    if not url:
        return None
    match = re.search(r'/firm/(\d+)', str(url))
    return match.group(1) if match else None

def get_progress_bar(iteration, total, length=20):
    """Генерация текстового прогресс-бара"""
    if total == 0:
        return f"[{'░' * length}] 0.0%"
    percent = ("{0:.1f}").format(100 * (iteration / float(total)))
    filled_length = int(length * iteration // total)
    bar = '█' * filled_length + '░' * (length - filled_length)
    return f"[{bar}] {percent}%"

async def bypass_museum(page):
    if "museum" in page.url:
        try:
            btn = page.get_by_text("Пропустить обновление")
            if await btn.count() > 0:
                await btn.first.click()
                await page.wait_for_timeout(random.randint(1000, 2000))
        except Exception:
            pass

async def process_firm(context, firm_id, url, ws, wb, file_path, lock, semaphore, state):
    """Параллельная обработка карточки заведения"""
    async with semaphore:
        page = None
        try:
            await asyncio.sleep(random.uniform(0.1, 0.5))

            page = await context.new_page()
            await page.route("**/*", lambda route: route.abort() 
                if route.request.resource_type in ["image", "stylesheet", "media", "font"] 
                else route.continue_())

            await page.goto(url, wait_until="domcontentloaded", timeout=25000)
            await bypass_museum(page)

            h1 = page.locator('h1')
            title = (await h1.first.inner_text()).split('\n')[0].strip() if await h1.count() > 0 else "Без названия"

            geo = page.locator('a[href*="/geo/"]')
            address = (await geo.first.inner_text()).replace('\n', ', ').strip() if await geo.count() > 0 else "Не указан"

            await page.evaluate("""() => {
                const btns = Array.from(document.querySelectorAll('button, div, span'));
                btns.forEach(b => {
                    const t = (b.innerText || '').toLowerCase();
                    if (t.includes('показать телефон') || t.includes('соцсети') || t.includes('показать контакт') || t.includes('ещё контакт')) {
                        try { b.click(); } catch(e) {}
                    }
                });
            }""")
            await page.wait_for_timeout(random.randint(500, 1000))

            tels = page.locator('a[href^="tel:"]')
            phones = []
            for i in range(await tels.count()):
                href = await tels.nth(i).get_attribute('href')
                if href:
                    num = href.replace('tel:', '').strip()
                    if num and num not in phones:
                        phones.append(num)
            phone_str = ", ".join(phones) if phones else "Не указан"

            raw_data = await page.evaluate("""() => {
                let results = [];
                document.querySelectorAll('a').forEach(a => { if (a.href) results.push(a.href); });
                document.querySelectorAll('div, span, a').forEach(el => {
                    let txt = el.innerText;
                    if (txt) {
                        txt = txt.trim();
                        if (txt.length > 4 && txt.length < 70 && !txt.includes('\\n') && !txt.includes(' ')) {
                            results.push(txt);
                        }
                    }
                });
                return Array.from(new Set(results));
            }""")

            websites = []
            bad_domains = ["2gis", "yandex", "google", "flamp", "apple", "play.google", "otello", "restoclub", "tomesto", "zoon", "sbermarket", "megamarket", "delivery-club", "eda.yandex", "mos.ru", "gosuslugi", "w3.org", "github.com", "yoo.money"]
            good_indicators = [".ru", ".com", ".рф", ".org", ".net", ".info", ".su", ".pro", ".moscow", ".agency", ".media", ".digital", "vk.com", "t.me", "wa.me", "taplink.cc"]

            for val in raw_data:
                val_lower = val.lower()
                if "redirect?url=" in val_lower:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(val).query)
                    if "url" in parsed:
                        val = urllib.parse.unquote(parsed["url"][0])
                        val_lower = val.lower()

                if any(ind in val_lower for ind in good_indicators) and not any(bd in val_lower for bd in bad_domains):
                    if "@" not in val:
                        if not val_lower.startswith("http"):
                            val = "https://" + val
                        if val not in websites:
                            websites.append(val)

            real_sites = [w for w in websites if not any(s in w.lower() for s in ['vk.com', 't.me', 'instagram.com', 'wa.me', 'whatsapp.com'])]
            socials = [w for w in websites if any(s in w.lower() for s in ['vk.com', 't.me', 'instagram.com', 'wa.me', 'whatsapp.com'])]
            site_str = real_sites[0] if real_sites else (socials[0] if socials else "Нет сайта")

            site_label = "[WEB]"
            if "t.me" in site_str or "tg://" in site_str:
                site_label = "[TG]"
            elif "vk.com" in site_str:
                site_label = "[VK]"
            elif "wa.me" in site_str or "whatsapp" in site_str:
                site_label = "[WA]"
            elif site_str == "Нет сайта":
                site_label = "[---]"

            async with lock:
                state["count"] += 1
                curr_no = state["count"]
                
                if phone_str != "Не указан":
                    state["phones_found"] = state.get("phones_found", 0) + 1
                if site_str != "Нет сайта":
                    state["sites_found"] = state.get("sites_found", 0) + 1

                ws.append([curr_no, title, address, phone_str, site_str, site_label, url])
                print(f"   [{curr_no}] {title} | 📞 {phone_str} | {site_label} {site_str}")

                state["unsaved"] = state.get("unsaved", 0) + 1
                if state["unsaved"] >= SAVE_EVERY_N:
                    try:
                        wb.save(file_path)
                        state["unsaved"] = 0
                        print(f"   💾 [Excel] Данные сохранены (Всего в файле: {curr_no})")
                    except PermissionError:
                        print("   ⚠️ [Excel] Файл занят другой программой! Данные копятся в памяти.")

        except asyncio.CancelledError:
            pass
        except PlaywrightError:
            async with lock:
                state["errors"] = state.get("errors", 0) + 1
        except Exception:
            async with lock:
                state["errors"] = state.get("errors", 0) + 1
        finally:
            if page:
                try:
                    await page.close()
                except Exception:
                    pass

async def main():
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    
    json_path = os.path.join(BASE_DIR, 'spisok_poiska.json')
    if not os.path.exists(json_path):
        print(f"❌ Ошибка: Файл {json_path} не найден! Создайте его перед запуском.")
        return
        
    with open(json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
        search_queries = data.get("queries", [])
        
    if not search_queries:
        print("❌ Ошибка: Список запросов в JSON пуст.")
        return

    city = "moscow"
    file_path = os.path.join(BASE_DIR, "база_2gis.xlsx")
    progress_file = os.path.join(BASE_DIR, "progress_b2b.txt")
    sheet_title = "База"
    
    # СОЗДАЕМ ФАЙЛ ПРОГРЕССА СРАЗУ ПРИ ЗАПУСКЕ (ЕСЛИ ЕГО НЕТ)
    if not os.path.exists(progress_file):
        with open(progress_file, "w", encoding="utf-8") as f:
            f.write("")
        print(f"📄 Создан файл отслеживания прогресса: progress_b2b.txt")

    MAX_PAGES = 15 
    CONCURRENCY_LIMIT = 4

    saved_ids = set()
    lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    
    if os.path.exists(file_path):
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active
        for row in ws.iter_rows(min_row=2, values_only=True):
            if row and len(row) >= 6:
                firm_id = get_firm_id(row[-1])
                if firm_id:
                    saved_ids.add(firm_id)
        results_count = ws.max_row - 1
        print(f"✅ Файл найден. Уже собрано уникальных карточек: {len(saved_ids)}")
    else:
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = sheet_title
        ws.append(["№", "Название", "Адрес", "Телефон", "Сайт", "Тип сайта", "URL"])
        wb.save(file_path)
        results_count = 0

    done_pages = set()
    if os.path.exists(progress_file):
        with open(progress_file, "r", encoding="utf-8") as f:
            done_pages = set(line.strip() for line in f if line.strip())

    state = {
        "count": results_count, 
        "duplicates": 0, 
        "errors": 0, 
        "unsaved": 0,
        "phones_found": 0,
        "sites_found": 0
    }
    start_time = time.time()
    queries_done_this_run = 0

    def format_eta(seconds):
        seconds = max(0, int(seconds))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        if h:
            return f"{h}ч {m}м"
        return f"{m}м {s}с"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=False, 
            args=["--disable-blink-features=AutomationControlled"]
        )
        
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1280, "height": 720}
        )
        
        main_page = await context.new_page()

        try:
            for q_idx, search_query in enumerate(search_queries, 1):
                if f"{search_query}|DONE" in done_pages:
                    print(f"⏩ Пропущен район (полностью готов): {search_query}")
                    continue
                    
                total_elapsed = time.time() - start_time
                avg_per_query = total_elapsed / queries_done_this_run if queries_done_this_run > 0 else 0
                remaining_queries = len(search_queries) - q_idx + 1
                eta = format_eta(remaining_queries * avg_per_query) if queries_done_this_run > 0 else "вычисляется..."
                
                print(f"\n==================================================")
                print(f"🔎 Запрос [{q_idx}/{len(search_queries)}]: '{search_query}'")
                print(f"📈 Прогресс районов: {get_progress_bar(q_idx - 1, len(search_queries))} | Осталось: ~{eta}")
                print(f"==================================================")
                
                encoded_query = urllib.parse.quote(search_query)
                query_start_count = state["count"]
                query_start_duplicates = state["duplicates"]
                query_start_phones = state.get("phones_found", 0)
                query_start_sites = state.get("sites_found", 0)
                query_start_time = time.time()

                for page_num in range(1, MAX_PAGES + 1):
                    page_key = f"{search_query}|page|{page_num}"
                    if page_key in done_pages:
                        print(f"   ⏩ Страница {page_num} уже была обработана ранее, пропускаем...")
                        continue

                    print(f"\n   📄 Страница {page_num} из {MAX_PAGES} (макс.)")
                    
                    search_url = f"https://2gis.ru/{city}/search/{encoded_query}/page/{page_num}"
                    
                    try:
                        await main_page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                        await bypass_museum(main_page)
                        await main_page.wait_for_timeout(1500) 

                        # --- НАДЕЖНЫЙ СКРОЛЛ К ПОСЛЕДНЕЙ КАРТОЧКЕ ---
                        empty_scrolls = 0 
                        for scroll_step in range(5):
                            current_cards = main_page.locator('a[href*="/firm/"]')
                            current_count = await current_cards.count()
                            
                            if current_count >= 24:
                                break
                                
                            if current_count > 0:
                                last_card = current_cards.nth(current_count - 1)
                                try:
                                    await last_card.scroll_into_view_if_needed(timeout=2000)
                                    await last_card.hover(timeout=2000)
                                except Exception:
                                    pass
                                    
                            await main_page.wait_for_timeout(1000)
                            
                            new_count = await main_page.locator('a[href*="/firm/"]').count()
                            if new_count == current_count:
                                empty_scrolls += 1
                                if empty_scrolls >= 2:
                                    break
                            else:
                                empty_scrolls = 0

                    except Exception as e:
                        print(f"   ⚠️ Ошибка при загрузке страницы {page_num}: {e}")
                        continue

                    cards = main_page.locator('a[href*="/firm/"]')
                    card_count = await cards.count()
                    print(f"   🔍 Найдено карточек на странице: {card_count}")

                    if card_count == 0:
                        captcha_indicators = await main_page.locator("text=/робот|капча|captcha/i").count()
                        
                        if captcha_indicators > 0:
                            print('\a')
                            print("\n[!!!] ВНИМАНИЕ: 2ГИС выдал капчу (теневой бан).")
                            print("[!!!] У вас есть 5 минут, чтобы решить её вручную в открытом окне браузера!")
                            
                            resolved = False
                            for sec in range(300):
                                await asyncio.sleep(1)
                                if await main_page.locator('a[href*="/firm/"]').count() > 0:
                                    print("\n[+] Капча успешно решена! Продолжаем сбор...")
                                    resolved = True
                                    card_count = await main_page.locator('a[href*="/firm/"]').count()
                                    break
                                if sec > 0 and sec % 60 == 0:
                                    print(f"... осталось {5 - sec//60} мин ...")
                            
                            if not resolved:
                                print("\n[-] Время вышло. Капча не решена. Скрипт остановлен для сохранения прогресса.")
                                return 
                        else:
                            print(f"   🛑 Заведений больше нет. Выдача района завершена.")
                            with open(progress_file, "a", encoding="utf-8") as f:
                                f.write(f"{search_query}|DONE\n")
                            done_pages.add(f"{search_query}|DONE")
                            break

                    tasks = []
                    duplicates_on_page = 0
                    unparsed_on_page = 0
                    
                    for i in range(card_count):
                        href = await cards.nth(i).get_attribute('href')
                        if href:
                            firm_id = get_firm_id(href)
                            if not firm_id:
                                unparsed_on_page += 1
                                continue
                            if firm_id not in saved_ids:
                                clean = href.split('?')[0]
                                full_url = clean if clean.startswith('http') else f"https://2gis.ru{clean}"
                                saved_ids.add(firm_id)
                                tasks.append(process_firm(context, firm_id, full_url, ws, wb, file_path, lock, semaphore, state))
                            else:
                                duplicates_on_page += 1

                    state["duplicates"] += duplicates_on_page

                    if duplicates_on_page > 0:
                        print(f"   ♻️ Дубликатов на странице: {duplicates_on_page}")
                    if tasks:
                        print(f"   ➕ Новых карточек к обработке: {len(tasks)}")

                    errors_before_page = state.get("errors", 0)
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    page_errors = state.get("errors", 0) - errors_before_page
                    if page_errors > 0:
                        print(f"   ⚙️ Ошибок при обработке на этой странице: {page_errors}")

                    if state["unsaved"] > 0:
                        try:
                            wb.save(file_path)
                            state["unsaved"] = 0
                        except PermissionError:
                            pass

                    with open(progress_file, "a", encoding="utf-8") as f:
                        f.write(f"{page_key}\n")
                    done_pages.add(page_key)

                    # --- УМНАЯ ОСТАНОВКА ---
                    if card_count < 12:
                        print(f"   🛑 Меньше 12 карточек на странице. Район полностью выгружен.")
                        with open(progress_file, "a", encoding="utf-8") as f:
                            f.write(f"{search_query}|DONE\n")
                        done_pages.add(f"{search_query}|DONE")
                        break
                    
                    if card_count > 0 and duplicates_on_page == card_count:
                        print(f"   🛑 2ГИС пошел по кругу (100% дубликатов на странице). Переходим к следующему району.")
                        with open(progress_file, "a", encoding="utf-8") as f:
                            f.write(f"{search_query}|DONE\n")
                        done_pages.add(f"{search_query}|DONE")
                        break

                query_new = state["count"] - query_start_count
                query_dupes = state["duplicates"] - query_start_duplicates
                query_elapsed = time.time() - query_start_time
                queries_done_this_run += 1
                
                q_phones = state.get("phones_found", 0) - query_start_phones
                q_sites = state.get("sites_found", 0) - query_start_sites
                ph_pct = (q_phones / query_new * 100) if query_new > 0 else 0
                st_pct = (q_sites / query_new * 100) if query_new > 0 else 0
                
                print(f"\n🏁 Итог по '{search_query}':")
                print(f"   ✅ Новых: {query_new} | ♻️ Дубликатов: {query_dupes} | ⏱️ Заняло: {query_elapsed:.0f}с")
                print(f"   📊 Качество данных: 📞 Телефоны {ph_pct:.1f}% | 🌐 Сайты {st_pct:.1f}%")

                if q_idx < len(search_queries):
                    pause_time = random.uniform(5.0, 10.0)
                    print(f"\n⏳ Ждем {pause_time:.1f} сек перед следующим районом для безопасности...")
                    await asyncio.sleep(pause_time)

        except asyncio.CancelledError:
            pass
        finally:
            if state.get("unsaved", 0) > 0:
                try:
                    wb.save(file_path)
                    state["unsaved"] = 0
                except PermissionError:
                    print("\n⚠️ Не удалось сохранить финальный xlsx — файл открыт в Excel!")

            total_elapsed = time.time() - start_time
            print(f"\n==================================================")
            print(f"📊 ИТОГО ЗА СЕССИЮ: собрано новых {state['count'] - results_count}, всего в файле {state['count']}")
            print(f"♻️ Дубликатов пропущено: {state['duplicates']} | ⚙️ Ошибок: {state.get('errors', 0)}")
            print(f"⏱️ Время работы: {format_eta(total_elapsed)}")
            print(f"==================================================")

            try:
                await browser.close()
            except Exception:
                pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n[!] Парсинг остановлен пользователем (Ctrl+C). Прогресс сохранен!")
