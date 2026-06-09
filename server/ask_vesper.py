# ─────────────────────────────────────────
# ASK VESPER v3
# Query Vesper using your life data (via HTTP)
# Run: python3 pipelines/ask_vesper.py [--smart]
# --smart: uses qwen3 for complex reasoning
# ─────────────────────────────────────────

import sys
import ollama
sys.path.append('/home/tanmay/vesper')
from pipelines.memory_client import search_memory, get_memory_count
from config import PRIMARY_MODEL, SMART_MODEL, USER_NAME

SYSTEM_PROMPT = f"""You are Vesper, {USER_NAME}'s personal AI assistant with access to their real life data:
- Emails (Gmail inbox + sent)
- iMessages and WhatsApp conversations
- Browser history
- Calendar events
- Contacts
- Local files
- Screen recordings and audio (screenpipe)

Be direct, concise, and useful. When referencing memories, cite them naturally.
If you don't have relevant data, say so briefly — don't make things up."""

# Keywords that suggest a complex query → use smart model
SMART_TRIGGERS = [
    "analyze", "summarize", "explain", "compare", "pattern",
    "who do i", "how often", "what did i", "when did i",
    "find all", "list all", "tell me about", "what are my",
    "overall", "generally", "typically", "trend"
]

def should_use_smart(question):
    q = question.lower()
    return any(trigger in q for trigger in SMART_TRIGGERS) or len(question.split()) > 10

def ask(question, force_smart=False):
    memories = search_memory(question, n=10)
    context = (
        "\n---\n".join(memories)
        if memories
        else "No relevant memories found for this query."
    )
    
    model = SMART_MODEL if (force_smart or should_use_smart(question)) else PRIMARY_MODEL
    
    prompt = f"""RELEVANT MEMORIES FROM YOUR LIFE:
{context}

Question: {question}

Answer using the memories above when relevant. Be concise and helpful."""

    print(f"\n[Vesper | {model}]: ", end="", flush=True)
    
    response = ollama.chat(
        model=model,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": prompt}
        ],
        stream=True,
        options={"temperature": 0.3, "num_predict": 800}
    )
    for chunk in response:
        print(chunk['message']['content'], end="", flush=True)
    print("\n")

if __name__ == "__main__":
    force_smart = "--smart" in sys.argv
    count = get_memory_count()
    print(f"Vesper v3 ready — {count} memories | {PRIMARY_MODEL} (fast) / {SMART_MODEL} (smart)")
    print("Type 'quit' to exit. Append --smart to force smart model.\n")
    while True:
        try:
            question = input("You: ").strip()
            if question.lower() in ['quit', 'exit', 'bye']:
                break
            if question:
                ask(question, force_smart=force_smart)
        except (KeyboardInterrupt, EOFError):
            break
