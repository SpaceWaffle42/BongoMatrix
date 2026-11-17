"""The Help guide for getting an OpenWeather API key. Shown behind the Help buttons on the Weather
screen's editor and in Settings > Weather."""

HELP = {
    "title": "Getting an OpenWeather API key",
    "required": True,
    "intro": "The weather comes from OpenWeather. It's free, but it needs a key: a long code that tells "
             "OpenWeather the requests come from you. You get one in a few minutes, and enter it here once "
             "for every Weather screen.",
    "steps": [
        "Make a free account at openweathermap.org (the first link below) and confirm your email address.",
        "Open My API keys (the second link below). You may need to log in again.",
        "Copy the key called Default (32 letters and numbers). No key there? Type a name, like "
        "`Bongo Matrix`, and press Generate.",
        "Back here, go to Settings > Weather, paste the key and press Save.",
        "A new key can take up to two hours to start working. Until it does, the Weather screens "
        "are skipped; they show the weather by themselves once it works.",
    ],
    "tips": [
        "The free plan is all you need, and it doesn't ask for a card.",
        "Keep your key to yourself, like a password. It's kept on the Matrix only, and never shown again here.",
        "Still says the key isn't working after a few hours? Copy it again from My API keys, whole, and save it again.",
    ],
    "credit": {"image": "/attribution/openweather_logo.webp", "url": "https://openweathermap.org",
               "text": "Weather data provided by OpenWeather"},
    "links": [
        {"label": "OpenWeather sign-up", "url": "https://home.openweathermap.org/users/sign_up"},
        {"label": "My API keys", "url": "https://home.openweathermap.org/api_keys"},
        {"label": "Settings > Weather", "route": "/settings"},
    ],
}
