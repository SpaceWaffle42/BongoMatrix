"""The Help guide for setting up Spotify, written for someone who has never made a developer app.
Shown behind the Help buttons on the Spotify screen's editor and in Settings > Spotify.
Checked against Spotify for Developers as it is after the February 2026 changes (one Development Mode
app per person, the app's maker needs Premium, up to five users per app)."""
from .account import REDIRECT

HELP = {
    "title": "Setting up Spotify",
    "required": True,
    "intro": "Spotify only lets a home gadget like your Matrix see what you're playing through a small "
             "\"app\" that you make on Spotify's developer website. It's free, needs no coding, takes about "
             "five minutes, and you only do it once. You need Spotify Premium to make the app.",
    "steps": [
        "Open the Spotify developer dashboard (the link below) and log in with your normal Spotify account. "
        "The first time, tick the box to accept the developer terms.",
        "Press Create app. If Spotify asks you to confirm your email address first, do that, then come back.",
        "Fill in the form. App name: `Bongo Matrix`. App description: `My LED matrix`. Leave Website empty.",
        f"In Redirect URIs paste `{REDIRECT}` exactly as it is here, then press Add. "
        "It has to be 127.0.0.1, not localhost.",
        "Under \"Which API/SDKs are you planning to use?\" tick Web API. Tick the box to agree to the terms, "
        "then press Save.",
        "Your app's page opens. Press Settings and copy the Client ID (32 letters and numbers). You don't "
        "need the Client secret: leave it alone.",
        "Back here in the dashboard, go to Settings > Spotify, paste the Client ID and press Sign in with "
        "Spotify. Spotify opens in a new tab: log in if asked, and press Agree if it asks.",
        "That tab then goes to a page that won't load (\"This site can't be reached\"). That's meant to "
        "happen. Copy the whole address from that tab's address bar (it starts with "
        f"{REDIRECT}?code= and goes on for a long way; on a phone, tap the address bar and select all "
        "of it), come back to this tab, paste it into \"The address it went to\" and press Connect.",
    ],
    "tips": [
        "Made a Spotify developer app before? Spotify allows each person just one: use that one instead. "
        "Open it, press Settings, then Edit, add the Redirect URI above, make sure Web API is ticked, "
        "and press Save.",
        "If the Premium on the account that made the app ends, Spotify stops the app working until it's back.",
        "Signing the Matrix in to someone else's Spotify? The app's maker adds them first: on the app's page "
        "press Settings, open the User Management tab, enter their name and Spotify email, and press Add "
        "user (up to five people).",
        "Paste the address straight after you approve. If it says the sign-in has expired, press Sign in "
        "with Spotify again and start from there.",
        "\"Spotify refused the sign-in\" usually means the Redirect URI on your app isn't exactly the one "
        "above (fix it on the app's page: Settings, then Edit, then Save), or the address you pasted was an "
        "old one. Either way, press Sign in with Spotify again.",
        "The Spotify screens only show while something is playing; otherwise they hand over to the next "
        "screen.",
    ],
    "links": [
        {"label": "Spotify developer dashboard", "url": "https://developer.spotify.com/dashboard"},
        {"label": "Settings > Spotify", "route": "/settings"},
    ],
}
