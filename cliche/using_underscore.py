import sys

UNDERSCORE_DETECTED = False
if len(sys.argv) > 1:
    UNDERSCORE_DETECTED = "_" in sys.argv[1]
for x in sys.argv[1:]:
    if x.startswith("-") and "_" in x:
        UNDERSCORE_DETECTED = True
