import re
import random


def spin(text: str) -> str:
    """Recursively resolve innermost {option1|option2} spintax patterns."""
    pattern = re.compile(r'\{([^{}]*)\}')
    while pattern.search(text):
        text = pattern.sub(
            lambda m: random.choice(m.group(1).split('|')),
            text
        )
    return text


def generate_variations(template: str, n: int) -> list:
    return [spin(template) for _ in range(n)]
