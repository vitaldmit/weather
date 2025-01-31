import json
import logging
import hashlib
import urllib.parse
import requests

from django.shortcuts import render
from django.utils import timezone
from django.core.cache import cache
from django.http import JsonResponse
from django.core.serializers.json import DjangoJSONEncoder
from django.db.models import Count
from rest_framework.views import APIView
from rest_framework.response import Response

from .models import SearchHistory
from .serializers import CitySearchCountSerializer


logger = logging.getLogger(__name__)


def home(request):
    """
    Отображает домашнюю страницу приложения погоды, обрабатывает вводимые пользователем данные и отображает данные о погоде.

    Функция `home` отвечает за следующее:
    - Проверка наличия у пользователя активной сессии и создание новой, если нет.
    - Получение предпочтительных единиц измерения температуры пользователя из сеанса.
    - Обработка пользовательского ввода города для поиска либо из отправки формы, либо из параметра GET.
    - Выполнение поиска по геокодированию для определения широты и долготы запрошенного города.
    - Вызов функции `get_weather` для получения данных о погоде для заданных координат.
    - Обновление истории поиска в базе данных.
    - Отображение шаблона `home.html` с полученными данными о погоде, историей поиска и любыми сообщениями об ошибках.
    """
    weather_data = None
    error_message = None

    if not request.session.session_key:
        request.session.save()
    session_key = request.session.session_key

    units = request.session.get('units', 'C')

    if request.method == 'POST':
        city = request.POST.get('city')
        units = request.POST.get('units', units)
        request.session['units'] = units
    elif request.method == 'GET' and 'city' in request.GET:
        city = request.GET.get('city')
    else:
        city = None

    if city:
        logger.info(f"Searching for city: {city}")

        city_name = city.split(',')[0].strip()

        encoded_city = urllib.parse.quote(city_name)
        geocoding_url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded_city}&count=5&language=ru&format=json"

        try:
            geocoding_response = requests.get(geocoding_url, timeout=5)
            geocoding_response.raise_for_status()
            geocoding_data = geocoding_response.json()

            logger.info(f"Geocoding API response: {geocoding_data}")

            if geocoding_data.get('results'):
                location = next((result for result in geocoding_data['results'] 
                                if result.get('country') == 'Russia'), 
                                geocoding_data['results'][0])

                logger.info(f"Selected location: {location}")

                latitude = location['latitude']
                longitude = location['longitude']
                full_city_name = location['name']
                if location.get('admin1'):
                    full_city_name += f", {location['admin1']}"
                if location.get('country'):
                    full_city_name += f", {location['country']}"

                logger.info(f"Fetching weather for: {full_city_name} ({latitude}, {longitude})")

                weather_data = get_weather(latitude, longitude, units)

                if weather_data:
                    logger.info("Weather data successfully retrieved")
                    SearchHistory.objects.update_or_create(
                        session_key=session_key,
                        city=full_city_name,
                        defaults={'search_date': timezone.now(), 'units': units}
                    )
                else:
                    logger.error("Failed to retrieve weather data")
                    error_message = "Не удалось получить данные о погоде. Пожалуйста, попробуйте позже."
            else:
                logger.warning(f"City not found: {city}")
                error_message = "Город не найден. Пожалуйста, проверьте название и попробуйте снова."
        except requests.RequestException as e:
            logger.error(f"Error fetching geocoding data: {e}")
            error_message = "Произошла ошибка при получении данных о местоположении. Пожалуйста, попробуйте позже."
        except ValueError as e:
            logger.error(f"Error parsing geocoding JSON: {e}")
            error_message = "Произошла ошибка при обработке данных о местоположении. Пожалуйста, попробуйте позже."

    # Получаем историю поисков и последний поиск после обновления
    search_history = SearchHistory.objects.filter(session_key=session_key)[:5]
    last_search = search_history.first()

    context = {
        'weather_data': weather_data,
        'last_search': last_search,
        'search_history': search_history,
        'error_message': error_message,
        'units': units,
    }
    if weather_data:
        context['weather_data_json'] = json.dumps(weather_data, cls=DjangoJSONEncoder)
    return render(request, 'weather/home.html', context)


def get_weather(latitude, longitude, units='C'):
    """
    Извлекает прогноз погоды для заданных координат широты и долготы и возвращает данные в виде словаря.

    Args:
        latitude (float): координата широты.
        longitude (float): координата долготы.
        units (str, optional): единицы измерения температуры: «C» для Цельсия или «F» для Фаренгейта. По умолчанию «С».

    Returns:
        dict: словарь, содержащий данные прогноза погоды, или «Нет», если возникает ошибка.
    """
    logger.info(f"Getting weather for coordinates: {latitude}, {longitude}, units: {units}")
    cache_key = hashlib.md5(f"{latitude}_{longitude}_{units}".encode()).hexdigest()
    cached_data = cache.get(cache_key)
    if cached_data:
        return cached_data

    temperature_unit = 'celsius' if units == 'C' else 'fahrenheit'
    url = f"https://api.open-meteo.com/v1/forecast?latitude={latitude}&longitude={longitude}&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,weathercode&temperature_unit={temperature_unit}&timezone=auto&forecast_days=7"
    try:
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        zipped_data = zip(
            data['daily']['time'],
            data['daily']['temperature_2m_max'],
            data['daily']['temperature_2m_min'],
            data['daily']['precipitation_sum'],
            data['daily']['weathercode']
        )
        data['daily']['zipped_data'] = list(zipped_data)
        data['units'] = units

        cache.set(cache_key, data, timeout=1800)  # 30 минут

        return data
    except requests.RequestException as e:
        logger.error(f"Error fetching weather data: {e}")
        return None
    except ValueError as e:
        logger.error(f"Error parsing weather JSON: {e}")
        return None


def city_autocomplete(request):
    """
    Предоставляет конечную точку API для автозаполнения названий городов с помощью службы геокодирования Nominatim.

    Эта функция просмотра обрабатывает запросы к конечной точке `/city-autocomplete/`, которая принимает параметр запроса `term` для поиска названий городов. Он отправляет запрос к Nominatim API, получает 5 наиболее подходящих названий городов и возвращает их в виде ответа JSON.

    Args:
        request (django.http.request.HttpRequest): входящий HTTP-запрос.

    Returns:
        django.http.response.JsonResponse: ответ JSON, содержащий список автоматически заполненных названий городов.
    """
    term = request.GET.get('term', '')
    url = f"https://nominatim.openstreetmap.org/search?format=json&q={term}"
    headers = {
        'User-Agent': 'WeatherApp/1.0 (https://yourwebsite.com; yourname@example.com)'
    }
    try:
        # time.sleep(1)  # Добавляем задержку в 1 секунду
        response = requests.get(url, headers=headers, timeout=5)
        response.raise_for_status()
        data = response.json()
        results = [item['display_name'] for item in data[:5]]
    except requests.RequestException as e:
        logger.error(f"Error fetching data from Nominatim: {e}")
        results = []
    except ValueError as e:
        logger.error(f"Error parsing JSON from Nominatim: {e}")
        results = []
    return JsonResponse(results, safe=False)


class CitySearchCountView(APIView):
    """
    Предоставляет конечную точку API для получения наиболее частого количества поисков по городам из модели `SearchHistory`.
    Класс `CitySearchCountView` — это представление API, которое возвращает список самых популярных городов при поиске, упорядоченных по количеству в порядке убывания.
    Данные ответа сериализуются с помощью `CitySearchCountSerializer` и возвращаются в виде ответа JSON.
    """
    def get(self, request):
        city_counts = SearchHistory.objects.values('city').annotate(count=Count('city')).order_by('-count')
        serializer = CitySearchCountSerializer(city_counts, many=True)
        return Response(serializer.data)
