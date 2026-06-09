#!/bin/bash
EXPORTS="$HOME/vesper_agent/exports"
mkdir -p "$EXPORTS"

# Export contacts via osascript
osascript << 'SCRIPT'
set output to ""
tell application "Contacts"
    repeat with p in people
        set pName to name of p
        set phones to {}
        set emails to {}
        try
            repeat with ph in phones of p
                set end of phones to (value of ph as string)
            end repeat
        end try
        try
            repeat with em in emails of p
                set end of emails to (value of em as string)
            end repeat
        end try
        set phoneStr to ""
        repeat with ph in phones
            set phoneStr to phoneStr & ph & ","
        end repeat
        set emailStr to ""
        repeat with em in emails
            set emailStr to emailStr & em & ","
        end repeat
        set output to output & pName & "|" & phoneStr & "|" & emailStr & "\n"
    end repeat
end tell
return output
SCRIPT
