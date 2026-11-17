"use client";

import { ArrowUp } from "lucide-react";
import { type FormEvent, type KeyboardEvent, useState } from "react";
import { Button } from "@/components/ui/button";

/** The message box. Enter sends; Shift+Enter adds a line. */
export function Composer({
  onSend,
  disabled = false,
  placeholder = "Ask Kairos anything…",
}: {
  onSend: (text: string) => Promise<boolean>;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const blocked = disabled || sending || text.trim() === "";

  async function submit(event?: FormEvent) {
    event?.preventDefault();
    if (blocked) return;
    setSending(true);
    const sent = await onSend(text.trim());
    setSending(false);
    if (sent) setText("");
  }

  function onKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (
      event.key === "Enter" &&
      !event.shiftKey &&
      !event.nativeEvent.isComposing
    ) {
      event.preventDefault();
      void submit();
    }
  }

  return (
    <form
      onSubmit={submit}
      className="flex items-end gap-2 rounded-2xl border bg-background p-2 shadow-sm"
    >
      <textarea
        aria-label="Message"
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        rows={1}
        placeholder={placeholder}
        className="field-sizing-content max-h-48 min-h-9 flex-1 resize-none bg-transparent px-2 py-1.5 outline-none placeholder:text-muted-foreground"
      />
      <Button type="submit" size="icon" disabled={blocked} aria-label="Send">
        <ArrowUp />
      </Button>
    </form>
  );
}
