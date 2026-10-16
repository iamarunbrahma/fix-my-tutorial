# Your first ChatGPT API call in Python

```bash
pip install openai
```

```python
import os
import openai

openai.api_key = os.environ["OPENAI_API_KEY"]
response = openai.ChatCompletion.create(
    model="gpt-3.5-turbo",
    messages=[{"role": "user", "content": "Say hello"}],
)
print(response["choices"][0]["message"]["content"])
```
