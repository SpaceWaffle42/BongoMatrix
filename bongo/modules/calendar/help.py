"""The Help guide for showing your own Google or iPhone calendar on the Calendar screens. Shown behind
the Help buttons on the Calendar screen's editor and in Settings > Calendars. Checked against Google's
"Sync your calendar with computer programs" page and the iOS Calendar app as they are in Nov 2026."""

HELP = {
    "title": "Showing your Google or iPhone calendar",
    "intro": "The Matrix can show the events from your own calendars beside the dates you type in, on every "
             "Calendar screen and countdown. It reads each calendar from a private link, so there's nothing to "
             "sign in to and nothing to install. It only reads: nothing on the Matrix ever changes your calendar. "
             "Changes show up within about 15 minutes, the next time a Calendar screen comes round.",
    "steps": [
        "Google Calendar (this part needs a computer: the phone app doesn't show the link). Go to "
        "calendar.google.com and sign in. Press the gear at the top right, then Settings.",
        "On the left, under \"Settings for my calendars\", press the calendar you want on the Matrix, then "
        "Integrate calendar.",
        "Find \"Secret address in iCal format\" (it ends in basic.ics) and copy it. Treat it like a password: "
        "anyone who has it can read that calendar.",
        "iPhone or iPad (iCloud calendars). Open the Calendar app and tap Calendars at the bottom. Under iCloud, "
        "tap the (i) next to the calendar you want.",
        "Turn on Public Calendar, then tap Share Link and Copy. The link starts with webcal://. Anyone who has it "
        "can see that calendar, but nobody can find it without the link.",
        "Here in the dashboard, go to Settings > Calendars. Give the calendar a name (like Family or Bins), pick a "
        "colour, paste the link and press Add calendar. The Matrix reads it straight away and says how many "
        "events it found, or what's wrong with the link.",
        "That's it: every Calendar screen with \"Linked calendars\" ticked (it is unless you untick it) now shows those events "
        "too. Add one link for each calendar you want, up to eight.",
    ],
    "tips": [
        "The link only lives on the Matrix. Once it's saved the dashboard never shows it again, and it never goes "
        "in the logs. To change it, remove the calendar and add it again.",
        "Pasted your Google link somewhere by mistake? On the same Integrate calendar page, press Reset next to "
        "the secret address. The old link stops working at once: add the new one here.",
        "Only want some events from a busy calendar? Fill in \"Only events with\" when adding it, e.g. \"birthday, "
        "bins, holiday\": then only events with one of those words in the title show.",
        "Private events (marked private in Google) are left out. Tick \"Show private events as Busy\" to show them "
        "as Busy, without their details.",
        "A work or school Google account may hide the secret address: that's the account's admin, not the Matrix. "
        "Use a personal calendar, or ask them.",
        "An iPhone calendar has to be an iCloud one. Calendars from other accounts on the phone (Google, Exchange) "
        "can't be made public there: add those from their own website instead.",
        "Outlook and most other calendar apps have the same kind of link (look for \"publish\", \"ICS\" or "
        "\"subscribe\"). Any link that starts with https:// or webcal:// and gives an .ics calendar works.",
        "If the internet drops, the Matrix keeps showing what it last read for up to a day.",
        "Event titles appear on the panel, so they're also in the live picture on the hub page (matrix.local), which "
        "anyone on your Wi-Fi can open. To keep that private, turn off \"Show what it's playing on the hub\" in "
        "Settings > Device.",
    ],
    "links": [
        {"label": "Google Calendar", "url": "https://calendar.google.com/calendar/r/settings"},
        {"label": "Google's help on the secret address", "url": "https://support.google.com/calendar/answer/37648"},
        {"label": "Settings > Calendars", "route": "/settings"},
    ],
}
