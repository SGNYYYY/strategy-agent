import os
import logging
import json
import itertools
from openai import OpenAI
from jinja2 import Environment, FileSystemLoader
from dotenv import load_dotenv

load_dotenv()

class BaseAgent:
    def __init__(self):
        # Support multiple API keys separated by commas for rotation
        api_keys_str = os.getenv("LLM_API_KEY", "")
        self.api_keys = [k.strip() for k in api_keys_str.split(',') if k.strip()]
        self.base_url = os.getenv("LLM_BASE_URL")
        self.model_name = os.getenv("LLM_MODEL_ID", "Qwen/Qwen3-8B")
        
        if not self.api_keys:
            logging.warning("No LLM_API_KEY found")
            self.clients = []
            self.client_cycle = None
        else:
            self.clients = [
                OpenAI(api_key=k, base_url=self.base_url) 
                for k in self.api_keys
            ]
            self.client_cycle = itertools.cycle(self.clients)

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
            if not self.clients or not self.client_cycle:
                logging.error("No available LLM clients configured")
                return None
                
            # Get next client in rotation
            client = next(self.client_cycle)
            
            response = client.chat.completions.create(
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
