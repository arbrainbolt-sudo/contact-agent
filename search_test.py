from ddgs import DDGS

results = DDGS().text("Anthropic press contact email", max_results=5)

for r in results:
    print(r["title"])
    print(r["href"])
    print()