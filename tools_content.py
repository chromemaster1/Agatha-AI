PLATFORM_SPECS = {
    "twitter": {
        "label": "Twitter/X",
        "char_limit": 280,
        "style": "punchy, conversational, at most 1-2 relevant hashtags, no more than one emoji",
    },
    "instagram": {
        "label": "Instagram",
        "char_limit": 2200,
        "style": "engaging caption with a hook in the first line, emoji-friendly, a block of 5-10 "
                 "hashtags on its own line at the very end",
    },
    "linkedin": {
        "label": "LinkedIn",
        "char_limit": 3000,
        "style": "professional but personable tone, short paragraphs/line breaks for readability, "
                 "at most 3 hashtags at the end",
    },
    "tiktok": {
        "label": "TikTok",
        "char_limit": 150,
        "style": "very short, hook-driven caption, casual tone, 3-5 trending-style hashtags",
    },
    "youtube": {
        "label": "YouTube",
        "char_limit": 5000,
        "style": "a compelling title under 70 characters, followed by a description with a hook, "
                 "key points, and a call to action",
    },
}


def default_platforms():
    return list(PLATFORM_SPECS.keys())


def normalize_platform_name(name: str) -> str:
    name = name.strip().lower()
    if name == "x":
        return "twitter"
    return name
