from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from bs4 import BeautifulSoup
from datetime import datetime
import pytz
import time
import re


def sanitize_filename(name):
    """
    Удаляет недопустимые символы из имени файла.
    На Windows недопустимы символы: \ / : * ? " < > |
    """
    return re.sub(r'[\\\/:*?"<>|]', '', name)


def parsing():
    # Настройка для работы с Chrome (опционально можно раскомментировать '--headless')
    options = webdriver.ChromeOptions()
    # options.add_argument('--headless')  # запуск без графического интерфейса
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')

    driver = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=options)

    # Получаем текущую дату в часовом поясе Europe/Moscow
    tz = pytz.timezone("Europe/Moscow")
    current_date = datetime.now(tz)

    # URL страницы рецептов
    url = "https://www.russianfood.com/recipes/"
    driver.get(url)
    print('test')
    html_content = driver.page_source
    soup = BeautifulSoup(html_content, 'lxml')
    print('test')

    # Задержка для корректной загрузки страницы
    time.sleep(10)

    # Массив для хранения данных (parsed data)
    data = []

    # Находим ссылки на категории блюд
    dishes_category = soup.find('div', class_='tags_block_all')
    if dishes_category:
        dishes_category = dishes_category.find('table', class_='rcpf')
    else:
        print("Не удалось найти блок с категориями блюд.")
        driver.quit()
        return []

    category_links = []
    if dishes_category:
        for x in dishes_category.find_all('dt'):
            link = x.find('a', class_='resList')
            if link and link.get('href'):
                category_links.append(link['href'])
    else:
        print("Не удалось найти таблицу с категориями.")

    for x in dishes_category.find_all('dt'):
        category_links.append(x.find('a', class_='resList')['href'])

    # В данном примере оставляем только одну категорию
    category_links = ['/recipes/bytype/?fid=2']

    for category in category_links:
        print('\n \n Открываем новую категорию \n \n')
        category_url = f"https://www.russianfood.com{category}&page=85#rcp_list"
        print(category_url)
        driver.get(category_url)
        html_content = driver.page_source
        soup = BeautifulSoup(html_content, 'lxml')
        time.sleep(2)

        page = int(category_url[category_url.find('page=') + 5:category_url.find('#')])
        page_confirm = 85

        while page == page_confirm:
            print(f'\n [{datetime.now(tz)}]   Переход на страницу {page}\n')
            dishes = soup.find('div', class_='recipe_list_new recipe_list_new2').find_all('div', class_='in_seen')

            for dish in dishes:
                title = dish.find('div', class_='title').find('h3').text.strip()
                print(title)

                desc = dish.find('div', class_='announce')
                if desc:
                    description = dish.find('div', class_='announce').text.strip()
                else:
                    description = 'Отсутствует'

                sub = dish.find('div', class_='announce_sub')
                if sub:
                    sub_description = dish.find('div', class_='announce_sub').find('span').text.strip()
                else:
                    sub_description = 'Отсутствует'

                link = f"https://www.russianfood.com{dish.find('a')['href']}"

                # Поиск изображения рецепта
                image_tag = dish.find('img', class_='round shadow')
                if image_tag and image_tag.get('src'):
                    image_src = image_tag['src']
                    if image_src.startswith("//"):
                        image_url = "https:" + image_src
                    else:
                        image_url = image_src
                    print("Ссылка на изображение:", image_url)
                else:
                    image_url = 'Изображение отсутствует'
                    print(image_url)

                # Сохраняем данные в виде кортежа
                data.append({
                    'data1': title,  # название
                    'data2': description,  # описание
                    'data3': image_url,  # картинка
                    'data4': link,  # ссылка
                    'sub_description': sub_description,
                    'current_date': current_date,
                    'status': 0
                })

            page_confirm += 1
            category_url = f"https://www.russianfood.com{category}&page={page_confirm}#rcp_list"
            driver.get(category_url)
            html_content = driver.page_source
            soup = BeautifulSoup(html_content, 'lxml')
            current_url = driver.current_url
            print(current_url)
            page = int(current_url[current_url.find('page=') + 5:current_url.find('#')])

    driver.quit()

    # Вывод переменной data, чтобы внешний код модуля мог её легко обнаружить
    return data


if __name__ == "__main__":
    print(parsing())

