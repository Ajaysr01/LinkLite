"""Base62 encoding and decoding utilities.

Base62 uses characters [0-9a-zA-Z] (62 symbols) to represent numeric values.
By adding an initial offset of 62^5 = 916,132,832, any integer >= 0 is
guaranteed to produce a string with at least 6 characters.
"""

ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
BASE = len(ALPHABET)  # 62

# 62^5 = 916,132,832. Lowest 6-character Base62 number ('100000')
BASE62_OFFSET = 916_132_832

# Mapping from character to value for O(1) decoding
CHAR_TO_INDEX = {char: idx for idx, char in enumerate(ALPHABET)}


def encode(num: int) -> str:
    """Encodes a non-negative integer into a Base62 string.

    Args:
        num: Non-negative integer.

    Returns:
        Base62 encoded string.

    Raises:
        ValueError: If num is negative.
    """
    if num < 0:
        raise ValueError("Cannot encode negative integers into Base62")
    if num == 0:
        return ALPHABET[0]

    digits: list[str] = []
    current = num
    while current > 0:
        current, rem = divmod(current, BASE)
        digits.append(ALPHABET[rem])

    return "".join(reversed(digits))


def decode(code: str) -> int:
    """Decodes a Base62 string back into an integer.

    Args:
        code: Base62 encoded string.

    Returns:
        Decoded integer.

    Raises:
        ValueError: If code contains invalid characters or is empty.
    """
    if not code:
        raise ValueError("Cannot decode empty Base62 string")

    result = 0
    for char in code:
        if char not in CHAR_TO_INDEX:
            raise ValueError(f"Invalid character '{char}' in Base62 string")
        result = result * BASE + CHAR_TO_INDEX[char]

    return result


def encode_id_with_offset(database_id: int) -> str:
    """Encodes a database sequence ID with the 6-character guarantee offset."""
    if database_id < 0:
        raise ValueError("Database ID cannot be negative")
    return encode(database_id + BASE62_OFFSET)


def decode_id_with_offset(code: str) -> int:
    """Decodes a 6+ character Base62 code back into the original database ID."""
    decoded_num = decode(code)
    if decoded_num < BASE62_OFFSET:
        raise ValueError(f"Decoded number {decoded_num} is below sequence offset {BASE62_OFFSET}")
    return decoded_num - BASE62_OFFSET
