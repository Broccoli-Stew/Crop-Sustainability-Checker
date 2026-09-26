"""
disease_model.py
----------------
Plant disease classification using the PlantVillage-trained
MobileNetV2 model on Hugging Face.

Images use the processor configuration saved with the model so
inference matches the model's expected resize, crop, and normalization.
"""

from PIL import Image
from transformers import AutoImageProcessor, AutoModelForImageClassification
import re
import torch
import torch.nn.functional as F

MODEL_NAME = "linkanjarad/mobilenet_v2_1.0_224-plant-disease-identification"


def load_model(local_files_only: bool = False):
    """
    Download/load the disease model.

    Returns:
        model
    """
    processor = AutoImageProcessor.from_pretrained(
        MODEL_NAME,
        local_files_only=local_files_only,
    )
    model = AutoModelForImageClassification.from_pretrained(
        MODEL_NAME,
        local_files_only=local_files_only,
    )
    model.image_processor = processor
    model.eval()

    return model


def _crop_match_key(plant: str) -> tuple[str, ...]:
    """Normalize plant names so labels such as `Pepper, bell` can be matched."""
    normalized = re.sub(r"[_.,()]+", " ", str(plant)).casefold()
    words = [word for word in normalized.split() if word not in {"plant", "leaf"}]
    words = [word[:-1] if len(word) > 4 and word.endswith("s") else word for word in words]
    if "corn" in words or "maize" in words:
        words = [word for word in words if word not in {"corn", "maize"}] + ["corn"]
    return tuple(sorted(words))


def _class_labels(model):
    id2label = model.config.id2label
    return [
        str(id2label.get(index, id2label.get(str(index), f"class {index}")))
        for index in range(model.config.num_labels)
    ]


def supported_plant_names(model) -> list[str]:
    """Return crop names represented by this model's class labels."""
    plants = {
        _parse_label(label)[0]
        for label in _class_labels(model)
        if _parse_label(label)[0] != "Unknown crop"
    }
    return sorted(plants, key=str.casefold)


def supports_plant(model, plant: str) -> bool:
    key = _crop_match_key(plant)
    return bool(key) and any(_crop_match_key(name) == key for name in supported_plant_names(model))


def predict(image: Image.Image, model, top_k: int = 3, plant_filter: str | None = None):
    """
    Run disease prediction.

    Returns:
        list of dictionaries containing plant, disease,
        raw label and confidence.
    """

    processor = getattr(model, "image_processor", None)
    if processor is None:
        raise ValueError("The leaf screening model was loaded without its image processor.")
    inputs = processor(images=image.convert("RGB"), return_tensors="pt")

    with torch.no_grad():
        outputs = model(**inputs)

    logits = outputs.logits[0]
    class_labels = _class_labels(model)
    class_indices = list(range(len(class_labels)))
    probabilities = F.softmax(logits.float(), dim=-1)
    overall_index = int(torch.argmax(probabilities).item())
    overall_plant, overall_disease = _parse_label(class_labels[overall_index])
    overall_confidence = float(probabilities[overall_index].item())

    if plant_filter:
        class_indices = [
            index
            for index, label in enumerate(class_labels)
            if _crop_match_key(_parse_label(label)[0]) == _crop_match_key(plant_filter)
        ]
        if not class_indices:
            raise ValueError(f"The model has no trained labels for {plant_filter}.")
    selected_probabilities = probabilities[class_indices]
    crop_probability = (
        float(selected_probabilities.sum().item()) if plant_filter else None
    )

    if plant_filter and crop_probability == 0.0:
        return [{
            "plant": plant_filter,
            "disease": "Unknown",
            "raw_label": "",
            "confidence": 0.0,
            "global_confidence": 0.0,
            "crop_probability": 0.0,
            "overall_plant": overall_plant,
            "overall_disease": overall_disease,
            "overall_confidence": overall_confidence,
            "low_crop_support": True,
        }]

    if plant_filter:
        ranked_probabilities = selected_probabilities / crop_probability
    else:
        ranked_probabilities = selected_probabilities

    k = min(top_k, ranked_probabilities.shape[0])

    top_probs, top_idxs = torch.topk(
        ranked_probabilities,
        k=k,
    )

    results = []

    for probability, result_index in zip(
        top_probs.tolist(),
        top_idxs.tolist(),
    ):
        index = class_indices[result_index]
        raw_label = class_labels[index]

        plant, disease = _parse_label(raw_label)
        result_crop_probability = crop_probability
        if not plant_filter:
            result_crop_indices = [
                label_index
                for label_index, label in enumerate(class_labels)
                if _crop_match_key(_parse_label(label)[0]) == _crop_match_key(plant)
            ]
            result_crop_probability = float(
                probabilities[result_crop_indices].sum().item()
            ) if result_crop_indices else 0.0

        results.append(
            {
                "plant": plant,
                "disease": disease,
                "raw_label": raw_label,
                "confidence": float(probability),
                "global_confidence": float(probabilities[index].item()),
                "crop_probability": result_crop_probability,
                "overall_plant": overall_plant,
                "overall_disease": overall_disease,
                "overall_confidence": overall_confidence,
                "low_crop_support": bool(
                    plant_filter and result_crop_probability < 0.001
                ),
            }
        )

    return results


def _parse_label(raw_label: str):
    """
    Convert model labels into (plant, disease).

    Labels may use the PlantVillage underscore convention, e.g.:

        "Tomato___Late_blight"       -> ("Tomato", "Late Blight")
        "Potato___Early_blight"      -> ("Potato", "Early Blight")
        "Apple___healthy"            -> ("Apple", "Healthy")
        "Tomato___Tomato_mosaic_virus" -> ("Tomato", "Tomato Mosaic Virus")

    Healthy natural-language labels such as "Healthy Grape Plant" are
    also recognized. Unrecognized formats are returned as Unknown so
    they cannot be presented as a crop or disease match.
    """

    label = str(raw_label).strip()

    if "___" in label:
        plant_raw, disease_raw = label.split("___", 1)
    else:
        natural_label = re.sub(r"[_]+", " ", label)
        natural_label = re.sub(r"\s+", " ", natural_label).strip()
        healthy_first = re.fullmatch(
            r"(?i)(?:healthy|normal)\s+(.+?)(?:\s+(?:leaf|plant))?", natural_label
        )
        healthy_last = re.fullmatch(
            r"(?i)(.+?)\s+(?:healthy|normal)(?:\s+(?:leaf|plant))?", natural_label
        )
        if healthy_first:
            return healthy_first.group(1).strip(), "Healthy"
        if healthy_last:
            return healthy_last.group(1).strip(), "Healthy"
        natural_lower = natural_label.casefold()
        if natural_lower == "cedar apple rust":
            return "Apple", "Cedar Apple Rust"

        plant_prefixes = (
            "Corn (Maize)", "Bell Pepper", "Apple", "Blueberry", "Cherry",
            "Grape", "Orange", "Peach", "Potato", "Raspberry", "Soybean",
            "Squash", "Strawberry", "Tomato",
        )
        for plant_name in plant_prefixes:
            match = re.fullmatch(
                rf"{re.escape(plant_name)}\s+(?:with\s+)?(.+)",
                natural_label,
                flags=re.IGNORECASE,
            )
            if not match:
                continue
            disease_name = match.group(1).strip()
            if plant_name == "Apple" and disease_name.casefold() == "scab":
                disease_name = "Apple Scab"
            if plant_name == "Tomato" and disease_name.casefold() in {
                "yellow leaf curl virus", "mosaic virus"
            }:
                disease_name = f"Tomato {disease_name}"
            return plant_name, disease_name.title()
        return "Unknown crop", "Unknown"

    plant = plant_raw.replace("_", " ").replace("(", "").replace(")", "").strip()

    disease_clean = disease_raw.replace("_", " ").strip()
    if disease_clean.lower() in {"healthy", "normal"}:
        disease = "Healthy"
    else:
        disease = disease_clean.title()

    return plant, disease
