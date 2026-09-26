"""Grounded agriculture chat using Gemini Cloud or a local Ollama server."""

from __future__ import annotations

import re

import requests

from crop_data import CROP_THRESHOLDS
from plant_health import BIO_FIRST_STEPS, GENERAL_STEPS
from polyculture import COMPANION_SYSTEMS


DEFAULT_GEMINI_MODEL = "gemini-3.5-flash"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"
DEFAULT_OLLAMA_URL = "http://localhost:11434/api/chat"


def _question_matches_crop(question: str, crop: str) -> bool:
    text = re.sub(r"[^a-z0-9]+", " ", question.casefold())
    crop_key = re.sub(r"[^a-z0-9]+", " ", crop.casefold()).strip()
    if crop_key and f" {crop_key} " in f" {text} ":
        return True
    if crop == "Corn (Maize)":
        return bool(re.search(r"\b(corn|maize)\b", text))
    aliases = {"Grapes": ("grape", "grapes"), "Green Bean": ("green bean", "beans")}
    return any(f" {alias} " in f" {text} " for alias in aliases.get(crop, ()))


def build_knowledge_context(question: str, screen_context: str = "") -> str:
    """Select relevant, bundled crop ranges, care notes, and cited pairings."""
    lines = [
        "Terrasense reference notes (screening information, not a field diagnosis):",
        f"General low-cost first steps: {GENERAL_STEPS}",
    ]

    matched_crops = [
        crop for crop in CROP_THRESHOLDS if _question_matches_crop(question, crop)
    ]
    for crop in matched_crops[:3]:
        data = CROP_THRESHOLDS[crop]
        lines.append(
            f"{crop}: broad app screening ranges are temperature {data['temp_c']} °C, "
            f"annual rainfall {data['rain_mm']} mm, and soil pH {data['ph']}. "
            f"App note: {data.get('notes', 'No additional note.')}"
        )

    question_lower = question.casefold()
    matched_diseases = []
    for disease, steps in BIO_FIRST_STEPS.items():
        disease_lower = disease.casefold()
        aliases = {
            "northern leaf blight": ("northern blight", "northern leaf blight"),
            "common rust": ("common rust", "corn rust", "maize rust"),
            "late blight": ("late blight",),
            "early blight": ("early blight",),
        }.get(disease_lower, (disease_lower,))
        if any(alias in question_lower for alias in aliases):
            matched_diseases.append((disease, steps))
    for disease, steps in matched_diseases[:3]:
        lines.append(f"Built-in first steps for {disease}: {steps}")

    if matched_crops:
        crop_set = set(matched_crops)
        for system in COMPANION_SYSTEMS:
            if crop_set.intersection(system["crops"]):
                lines.append(
                    f"Sourced companion planting example: {system['name']}. "
                    f"{system['why']} Management: {system['manage']} "
                    f"Reference: {system['source']}"
                )

    if screen_context:
        lines.append(f"Current Terrasense screen context: {screen_context}")

    return "\n".join(lines)


def build_system_prompt(language_name: str, knowledge_context: str) -> str:
    return f"""You are Croppy, Terrasense's agriculture information assistant.
Answer in {language_name}. Help with crop selection, growing conditions, soil, companion planting, pests, plant diseases, and cautious first steps.

Use the Terrasense reference notes below when relevant. Separate the app's broad screening ranges from locally verified recommendations. You may use general agricultural knowledge, but never invent citations or claim that the notes prove a diagnosis. Ask for the crop, country/region, plant age, and symptom pattern when these details would change the advice.

Be clear when you are uncertain. The image classifier is a limited screening model and cannot confirm a disease. Do not say a plant is healthy merely because the model returned a weak or uncertain class. Croppy cannot see uploaded photos; it only receives a short text summary of a current Terrasense result when relevant. For pesticides or other regulated treatments, do not invent a product, dose, or mixing instruction; tell the user to follow the locally approved product label and ask an agricultural extension specialist. For urgent crop loss, spreading disease, or food-safety concerns, recommend local agricultural guidance.

Keep answers practical and readable. When a supplied reference URL supports the answer, include it. If the question is unrelated to agriculture or this app, politely say what you can help with.

Terrasense reference notes:
{knowledge_context}
"""


def ask_croppy(
    *,
    provider: str,
    prompt: str,
    history: list[dict],
    system_prompt: str,
    api_key: str = "",
    gemini_model: str = DEFAULT_GEMINI_MODEL,
    ollama_url: str = DEFAULT_OLLAMA_URL,
    ollama_model: str = DEFAULT_OLLAMA_MODEL,
) -> str:
    """Send one chat turn to Gemini or to the configured local Ollama server."""
    recent_history = list(history[-12:])
    if not recent_history or (
        recent_history[-1].get("role") != "user"
        or recent_history[-1].get("content") != prompt
    ):
        recent_history.append({"role": "user", "content": prompt})

    if provider == "local":
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(
            {
                "role": "assistant" if item.get("role") == "assistant" else "user",
                "content": str(item.get("content", "")),
            }
            for item in recent_history
        )
        response = requests.post(
            ollama_url,
            json={
                "model": ollama_model,
                "messages": messages,
                "stream": False,
                "options": {"temperature": 0.25},
            },
            timeout=(5, 120),
        )
        response.raise_for_status()
        answer = response.json().get("message", {}).get("content", "").strip()
        if not answer:
            raise RuntimeError("The local AI server returned an empty answer.")
        return answer

    if provider != "cloud":
        raise ValueError("Choose either Cloud or Local AI mode.")
    if not api_key:
        raise RuntimeError("Croppy Cloud needs a Gemini API key.")

    contents = []
    for item in recent_history:
        role = "user" if item.get("role") == "user" else "model"
        content = str(item.get("content", "")).strip()
        if content:
            if contents and contents[-1]["role"] == role:
                contents[-1]["parts"].append({"text": content})
            else:
                contents.append({"role": role, "parts": [{"text": content}]})

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{gemini_model}:generateContent"
    )
    response = requests.post(
        url,
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json={
            "systemInstruction": {"parts": [{"text": system_prompt}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.25, "maxOutputTokens": 900},
        },
        timeout=(5, 120),
    )
    response.raise_for_status()
    data = response.json()
    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
    answer = "\n".join(part.get("text", "") for part in parts).strip()
    if not answer:
        reason = data.get("promptFeedback", {}).get("blockReason", "no response text")
        raise RuntimeError(f"Gemini could not provide a response ({reason}).")
    return answer
