"""Input validation utilities."""

import re
from typing import List

MAX_TAG_LENGTH = 50
MAX_TAGS_COUNT = 20
MAX_CUSTOM_FOLLOWS = 100
MAX_KEYWORD_LENGTH = 30

def validate_tags(tags_str: str) -> tuple[bool, str]:
    """Validate tags string. Returns (is_valid, error_message)."""
    if not tags_str or not tags_str.strip():
        return True, ""

    tags = [t.strip() for t in tags_str.split(",") if t.strip()]

    if len(tags) > MAX_TAGS_COUNT:
        return False, f"Maksimum {MAX_TAGS_COUNT} tag seçebilirsiniz."

    for tag in tags:
        if len(tag) > MAX_TAG_LENGTH:
            return False, f"Tag '{tag}' çok uzun (max {MAX_TAG_LENGTH} karakter)."
        if not re.match(r"^[\w\-\s]+$", tag):
            return False, f"Tag '{tag}' geçersiz karakterler içeriyor."

    return True, ""

def validate_custom_follows(follows_str: str) -> tuple[bool, str]:
    """Validate custom follows string. Returns (is_valid, error_message)."""
    if not follows_str or not follows_str.strip():
        return True, ""

    follows = [k.strip() for k in follows_str.split(",") if k.strip()]

    if len(follows) > MAX_CUSTOM_FOLLOWS:
        return False, f"Maksimum {MAX_CUSTOM_FOLLOWS} kelime takip edebilirsiniz."

    for keyword in follows:
        if len(keyword) > MAX_KEYWORD_LENGTH:
            return False, f"Kelime '{keyword}' çok uzun (max {MAX_KEYWORD_LENGTH} karakter)."
        if not re.match(r"^[\w\-\s]+$", keyword):
            return False, f"Kelime '{keyword}' geçersiz karakterler içeriyor."

    return True, ""

def validate_username(username: str) -> tuple[bool, str]:
    """Validate Telegram username. Returns (is_valid, error_message)."""
    if not username or not username.strip():
        return True, ""

    if len(username) > 32:
        return False, "Username çok uzun."

    return True, ""
