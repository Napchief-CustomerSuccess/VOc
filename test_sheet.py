import json
import os

with open('.env') as f:
    for line in f:
        if '=' in line:
            k, v = line.strip().split('=', 1)
            os.environ[k] = v

from sheets import get_pending_numbers

try:
    print(get_pending_numbers())
except Exception as e:
    import traceback
    traceback.print_exc()
