from anthropic import AsyncAnthropic

_client = AsyncAnthropic()


async def ask_claude(prompt: str) -> str:
    async with _client.messages.stream(
        model="claude-opus-4-8",
        max_tokens=1024,
        thinking={"type": "adaptive"},
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        message = await stream.get_final_message()

    for block in message.content:
        if block.type == "text":
            return block.text
    return ""
