# Counting words with collections.Counter

```python
from collections import Counter

words = "the quick brown fox jumps over the lazy dog the end".split()
print(Counter(words).most_common(1))
```

Output:

```
[('the', 3)]
```
