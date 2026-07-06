from __future__ import annotations

import json
from itertools import combinations

from .config import settings


ANIMALS = [
    ("Cow", "Moo Moo"),
    ("Dog", "Woof Woof"),
    ("Cat", "Meow"),
    ("Duck", "Quack Quack"),
    ("Sheep", "Baa Baa"),
    ("Horse", "Neigh"),
    ("Pig", "Oink Oink"),
    ("Lion", "Roar"),
    ("Elephant", "Trumpet"),
    ("Monkey", "Ooh Ooh"),
    ("Frog", "Ribbit"),
    ("Bird", "Tweet Tweet"),
    ("Goat", "Maa Maa"),
    ("Chicken", "Cluck Cluck"),
    ("Bear", "Growl"),
]

COLORS = [
    "Red", "Blue", "Yellow", "Green", "Orange", "Purple", "Pink", "Brown", "Black", "White",
    "Gray", "Gold", "Silver", "Cyan", "Magenta",
]

NUMBERS = [
    "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen", "Twenty",
]

SHAPES = [
    "Circle", "Square", "Triangle", "Rectangle", "Star", "Heart", "Oval", "Diamond", "Pentagon", "Hexagon",
    "Crescent", "Arrow",
]

FRUITS = [
    "Apple", "Banana", "Orange", "Mango", "Grapes", "Watermelon", "Strawberry", "Pineapple",
    "Peach", "Pear", "Cherry", "Kiwi", "Papaya", "Blueberry",
]

VEGETABLES = [
    "Carrot", "Tomato", "Potato", "Broccoli", "Corn", "Cucumber", "Onion", "Peas",
    "Pumpkin", "Spinach", "Cabbage", "Pepper",
]

VEHICLES = [
    "Car", "Bus", "Truck", "Train", "Airplane", "Boat", "Bicycle", "Motorcycle",
    "Helicopter", "Tractor", "Fire Truck", "Ambulance",
]

OPPOSITES = [
    ("Big", "Small"),
    ("Tall", "Short"),
    ("Hot", "Cold"),
    ("Fast", "Slow"),
    ("Happy", "Sad"),
    ("Up", "Down"),
    ("Open", "Closed"),
    ("Day", "Night"),
    ("Full", "Empty"),
    ("Wet", "Dry"),
]

SCIENCE = [
    "Sun", "Moon", "Earth", "Stars", "Clouds", "Rain", "Rainbow", "Plants", "Seeds", "Magnets",
    "Five Senses", "Weather", "Solar System", "Dinosaurs", "Sea Animals",
]

ALPHABET = [chr(code) for code in range(ord("A"), ord("Z") + 1)]


def slug(text: str) -> str:
    return (
        text.lower()
        .replace(" ", "_")
        .replace("-", "_")
        .replace("__", "_")
        .strip("_")
    )


def scene(label: str, line: str, topic_key: str, category: str) -> dict[str, str]:
    category_slug = slug(category)
    asset_category = {
        "animals": "animals",
        "colors": "colors",
        "numbers": "numbers",
    }.get(category_slug, f"generated/{category_slug}")
    return {
        "label": label,
        "line": line,
        "image": f"assets/{asset_category}/{slug(label)}.png",
        "image_prompt": (
            "Bright professional 3D cartoon kids learning illustration. "
            f"Main subject: {label}. Vertical 9:16, large centered subject, "
            "clean cheerful preschool background, no readable text, no watermark."
        ),
    }


def lesson(topic_key: str, title: str, category: str, scenes: list[dict[str, str]]) -> dict:
    return {
        "title": title,
        "category": category,
        "intro": f"Hello kids! Let's learn {category.lower()}.",
        "outro": "Great job! See you in the next lesson.",
        "scenes": scenes,
    }


def grouped(items: list, size: int = 5) -> list[list]:
    return [items[index:index + size] for index in range(0, len(items), size)]


def generated_lessons(limit: int = 1200) -> dict[str, dict]:
    lessons: dict[str, dict] = {}

    for index, group in enumerate(grouped(ANIMALS, 3), start=1):
        key = f"animal_sounds_{index:03}"
        lessons[key] = lesson(
            key,
            f"Animal Sounds {index}",
            "Animals",
            [scene(name, f"This is a {name}. A {name} says {sound}!", key, "Animals") for name, sound in group],
        )

    for index, group in enumerate(grouped(COLORS, 3), start=1):
        key = f"learn_colors_{index:03}"
        lessons[key] = lesson(
            key,
            f"Learn Colors {index}",
            "Colors",
            [scene(color, f"This color is {color}.", key, "Colors") for color in group],
        )

    for start in range(0, len(NUMBERS), 5):
        group = NUMBERS[start:start + 5]
        key = f"counting_{start + 1:02}_to_{start + len(group):02}"
        lessons[key] = lesson(
            key,
            f"Counting {start + 1} To {start + len(group)}",
            "Numbers",
            [scene(name, f"{name}. Can you say {name.lower()}?", key, "Numbers") for name in group],
        )

    for index, group in enumerate(grouped(SHAPES, 3), start=1):
        key = f"learn_shapes_{index:03}"
        lessons[key] = lesson(
            key,
            f"Learn Shapes {index}",
            "Shapes",
            [scene(shape, f"This shape is a {shape}.", key, "Shapes") for shape in group],
        )

    for category, items in [("Fruits", FRUITS), ("Vegetables", VEGETABLES), ("Vehicles", VEHICLES)]:
        for index, group in enumerate(grouped(items, 3), start=1):
            key = f"learn_{category.lower()}_{index:03}"
            lessons[key] = lesson(
                key,
                f"Learn {category} {index}",
                category,
                [scene(item, f"This is a {item}.", key, category) for item in group],
            )

    for index, group in enumerate(grouped(ALPHABET, 3), start=1):
        key = f"alphabet_letters_{index:03}"
        lessons[key] = lesson(
            key,
            f"Alphabet Letters {group[0]}-{group[-1]}",
            "Alphabet",
            [scene(letter, f"Letter {letter}. {letter} is for learning!", key, "Alphabet") for letter in group],
        )

    for index, group in enumerate(grouped(OPPOSITES, 2), start=1):
        key = f"learn_opposites_{index:03}"
        scenes = []
        for left, right in group:
            scenes.append(scene(left, f"{left} is the opposite of {right}.", key, "Opposites"))
            scenes.append(scene(right, f"{right} is the opposite of {left}.", key, "Opposites"))
        lessons[key] = lesson(key, f"Learn Opposites {index}", "Opposites", scenes)

    for index, group in enumerate(grouped(SCIENCE, 3), start=1):
        key = f"basic_science_{index:03}"
        lessons[key] = lesson(
            key,
            f"Basic Science {index}",
            "Basic Science",
            [scene(item, f"Let's learn about {item}.", key, "Science") for item in group],
        )

    variants = ["Fun", "Easy", "Quick", "Happy", "Smart", "Cute", "Simple", "Bright", "Daily", "Mini"]
    categories = [
        ("Animals", [name for name, _sound in ANIMALS]),
        ("Colors", COLORS),
        ("Numbers", NUMBERS),
        ("Shapes", SHAPES),
        ("Fruits", FRUITS),
        ("Vegetables", VEGETABLES),
        ("Vehicles", VEHICLES),
        ("Science", SCIENCE),
    ]
    for variant in variants:
        for category, items in categories:
            for combo_index, combo in enumerate(combinations(items, 3), start=1):
                if len(lessons) >= limit:
                    return lessons
                key = f"{slug(variant)}_{slug(category)}_{combo_index:04}"
                lessons[key] = lesson(
                    key,
                    f"{variant} {category} Lesson {combo_index}",
                    category,
                    [scene(item, f"This is {item}. Let's say {item}!", key, category) for item in combo],
                )
    return lessons


def load_all_lessons() -> dict[str, dict]:
    with settings.data_file.open("r", encoding="utf-8") as handle:
        curated = json.load(handle)
    bank = generated_lessons()
    bank.update(curated)
    return bank
