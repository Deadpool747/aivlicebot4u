"""Lightweight language heuristics for choosing and tracking conversation language."""

from __future__ import annotations

import re

MARATHI_NAME_TOKENS = {
    "aaditya",
    "ajinkya",
    "amol",
    "archana",
    "ashwini",
    "chinmay",
    "ganesh",
    "gaurav",
    "kishor",
    "madhuri",
    "mahesh",
    "manasi",
    "milind",
    "mugdha",
    "nilesh",
    "prasad",
    "prajakta",
    "sachin",
    "sandip",
    "shailesh",
    "shrikant",
    "swapnil",
    "tejas",
    "umesh",
    "vaibhav",
}

MARATHI_SURNAME_TOKENS = {
    "bagal",
    "bapat",
    "barve",
    "bhagat",
    "bhide",
    "deshmukh",
    "gadkari",
    "gaikwad",
    "jadhav",
    "joshi",
    "kadam",
    "kale",
    "karandikar",
    "kulkarni",
    "mane",
    "more",
    "pansare",
    "patil",
    "pawar",
    "ranade",
    "sathe",
    "sawant",
    "shinde",
    "thackeray",
    "wagh",
}


def infer_opening_language(customer_name: str) -> str:
    """Return `marathi` for Marathi-leaning names, otherwise `hindi`."""
    normalized_tokens = {
        token.strip(" .").lower()
        for token in customer_name.replace("-", " ").split()
        if token.strip(" .")
    }
    if normalized_tokens & MARATHI_NAME_TOKENS:
        return "marathi"
    if normalized_tokens & MARATHI_SURNAME_TOKENS:
        return "marathi"
    return "hindi"


HINDI_MARKERS = {
    "क्या",
    "है",
    "हाँ",
    "मैं",
    "आप",
    "आपका",
    "अभी",
    "क्यों",
    "नहीं",
    "सकते",
}

MARATHI_MARKERS = {
    "आहे",
    "आहेत",
    "तुमचं",
    "तुमचे",
    "मी",
    "माझं",
    "कडून",
    "बोलते",
    "बोलत",
    "हो",
    "नाही",
    "कृपया",
    "वाजता",
    "उद्या",
    "द्या",
    "सकाळी",
    "सायंकाळी",
}


def detect_conversation_language(text: str, fallback: str = "hindi") -> str:
    """Infer English, Hindi, or Marathi from the latest user utterance."""
    normalized = " ".join(text.split()).strip()
    if not normalized:
        return fallback

    latin_chars = len(re.findall(r"[A-Za-z]", normalized))
    devanagari_chars = len(re.findall(r"[\u0900-\u097F]", normalized))
    if latin_chars and latin_chars >= max(devanagari_chars, 4):
        return "english"

    tokens = set(normalized.split())
    marathi_score = sum(1 for token in tokens if token in MARATHI_MARKERS)
    hindi_score = sum(1 for token in tokens if token in HINDI_MARKERS)

    if "आहे" in normalized or "तुमचं" in normalized or "कडून" in normalized:
        marathi_score += 2
    if "वाजता" in normalized or "उद्या" in normalized or "सकाळी" in normalized:
        marathi_score += 2
    if "है" in normalized or "क्या" in normalized or "आप" in normalized:
        hindi_score += 2

    if marathi_score > hindi_score:
        return "marathi"
    if hindi_score > marathi_score:
        return "hindi"
    if devanagari_chars:
        return fallback if fallback in {"hindi", "marathi"} else "hindi"
    return fallback
