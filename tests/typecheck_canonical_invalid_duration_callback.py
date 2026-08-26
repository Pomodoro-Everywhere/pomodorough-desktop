from pomodorough.storage_canonical_validation import valid_canonical_timer


def zero_argument_duration() -> int:
    return 1


valid_canonical_timer({}, zero_argument_duration)
