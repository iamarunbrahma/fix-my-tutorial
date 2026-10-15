# Looping over DataFrame columns

```bash
pip install pandas
```

```python
import pandas as pd

df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
for col, values in df.iteritems():
    print(col, values.sum())
```
