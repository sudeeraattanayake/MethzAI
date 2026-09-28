from agent import get_agent
from langchain_core.messages import SystemMessage, HumanMessage
from database import init_db

init_db()
agent = get_agent("gpt-5-mini")

config = {
    "configurable": {
        "thread_id": "test_thread_id",
    }
}

for message_chunk, metadata in agent.stream(
    {"messages": [HumanMessage(
        content="What is my name")]},
    config=config,
    stream_mode="messages"
):
    if message_chunk.content:
        print(message_chunk.content, end="", flush=True)
