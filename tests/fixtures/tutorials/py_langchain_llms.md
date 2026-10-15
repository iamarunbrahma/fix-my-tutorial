# Getting started with LangChain

```bash
pip install langchain openai
```

```python
from langchain.llms import OpenAI

llm = OpenAI(temperature=0.7)
print(llm("Write a haiku about unit tests"))
```
