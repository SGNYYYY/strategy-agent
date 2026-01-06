import os
import logging
import json
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv

load_dotenv()

class BaseAgent:
    def __init__(self):
        self.api_key = os.getenv("LLM_API_KEY")
        self.base_url = os.getenv("LLM_BASE_URL")
        self.model_name = os.getenv("LLM_MODEL_ID", "Qwen/Qwen3-8B")
        
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url
        )
        extra_body = {
            # enable thinking, set to False to disable test
            "enable_thinking": False,
            # use thinking_budget to contorl num of tokens used for thinking
            # "thinking_budget": 4096
        }
        self.extra_body = extra_body
        
        self.jinja_env = Environment(loader=FileSystemLoader('prompts'))

    def call_llm(self, prompt, json_mode=False):
        try:
            response = self.client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"} if json_mode else None,
                extra_body=self.extra_body
            )
            content = response.choices[0].message.content
            if json_mode:
                return json.loads(content)
            return content
        except Exception as e:
            logging.error(f"LLM call failed: {e}")
            return None

    def render_prompt(self, template_name, **kwargs):
        template = self.jinja_env.get_template(template_name)
        return template.render(**kwargs)
