# VESPER EMAIL TOOLS
# Summarize emails and send via Gmail SMTP
# Called by ask_vesper.py or Open-WebUI tools

import smtplib, ssl, json, os, sys
sys.path.append('/home/tanmay/vesper')
from pipelines.memory import search_memory
from config import DATA_PATH, PRIMARY_MODEL
import ollama

EMAILS_FILE = f'{DATA_PATH}/emails/gmail_emails.json'

def summarize_new_emails(n=10):
    """Return a plain-text summary of the n most recent emails."""
    try:
        emails = json.load(open(EMAILS_FILE))
    except:
        return 'No emails found. Run export_gmail.sh on Mac first.'
    recent = [e for e in emails if e.get('category') == 'email_received'][:n]
    if not recent:
        return 'No recent emails found.'
    lines = []
    for e in recent:
        lines.append(
            f'- [{e["date"][:16]}] FROM {e["from_name"] or e["from"]} | '
            f'SUBJECT: {e["subject"]} | '
            f'{e["body"][:120].strip()}'
        )
    return '\n'.join(lines)

def ask_about_emails(question):
    """Use AI to answer a question about your emails."""
    memories = search_memory(question, n=8, category='email_received')
    if not memories:
        memories = search_memory(question, n=8)
    context = '\n'.join(memories) if memories else 'No relevant emails found.'
    prompt = f"""EMAIL CONTEXT:\n{context}\n\nQuestion: {question}\nAnswer:"""
    resp = ollama.chat(
        model=PRIMARY_MODEL,
        messages=[
            {'role':'system','content':'You are Vesper. Answer questions about Tanmay emails concisely.'},
            {'role':'user','content':prompt}
        ]
    )
    return resp['message']['content']

def draft_reply(original_subject, original_from, original_body, instruction=''):
    """Draft a reply to an email using AI."""
    prompt = f"""Draft a professional email reply.
Original from: {original_from}
Subject: {original_subject}
Original message: {original_body[:500]}
{f'Instructions: {instruction}' if instruction else ''}
Write only the reply body, no subject line:"""
    resp = ollama.chat(
        model=PRIMARY_MODEL,
        messages=[
            {'role':'system','content':'You are Vesper, writing emails on behalf of Tanmay Pramanick.'},
            {'role':'user','content':prompt}
        ]
    )
    return resp['message']['content']

if __name__ == '__main__':
    print('=== Recent Emails ===')
    print(summarize_new_emails(5))
