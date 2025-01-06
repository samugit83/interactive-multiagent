# helpers/utils.py

from openai import OpenAI
import logging
import os
import traceback  

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY environment variable is not set")

client = OpenAI(
    api_key=OPENAI_API_KEY 
)

def call_openai_model(prompt: str = None, model: str = "o1-mini") -> str:
    try:
        completion = client.chat.completions.create(
            model=model, 
            messages=[
                {"role": "user", "content": prompt},
            ]
        )

        answer = completion.choices[0].message.content.strip()
        return answer
    except Exception as e:
        logger.error(f"OpenAI API error: {str(e)}")
        logger.error(traceback.format_exc())  
        raise e