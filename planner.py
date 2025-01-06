
import json
import re
import os
import logging
import concurrent.futures
from typing import List, Dict
from prompts import (
    SYSTEM_PROMPT_AGENT_PLANNER, 
    JSON_CHAIN_EXAMPLE, 
    DIPENDENT_AGENT_PROMPT,
    AGGREGATOR_PROMPT
)
from models import call_openai_model
from agent_session_manager import AgentSessionManager
from agent_data_model import AgentDataModel

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", 6379))
REDIS_DB = int(os.getenv("REDIS_DB", 0))


class MemoryLogHandler(logging.Handler):
    def __init__(self, memory_logs: List[str], *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.memory_logs = memory_logs

    def emit(self, record):
        log_entry = self.format(record)
        self.memory_logs.append(log_entry)

class AgentPlanner:
    def __init__(
            self, 
            chat_history: List[Dict],
            **kwargs
    ):
        is_interactive = kwargs.get('is_interactive', True)
        self.logger = logging.getLogger(__name__)

        if is_interactive:
            session_id = kwargs.get('session_id')
            if not session_id:
                raise ValueError("session_id is required for interactive mode")
            self.session_manager = AgentSessionManager(
                redis_host=REDIS_HOST, 
                redis_port=REDIS_PORT, 
                db=REDIS_DB
            )
            logging.info('🟣 --------------------- Loading session: %s', f'planner-{session_id}')
            self.data = self.session_manager.load_session(f'planner-{session_id}')
            self.data.kwargs.update(kwargs)
            self.data.session_id = self.data.session_id or f"planner-{self.data.kwargs.get('session_id', None)}"
            self.data.is_interactive = self.data.is_interactive or self.data.kwargs.get('is_interactive', False)
            self.data.user_id = self.data.user_id or self.data.kwargs.get('user_id', None)
            self.data.chat_history = chat_history
            self.data.start_system_prompt = self.data.start_system_prompt or self.data.kwargs.get(
                'start_system_prompt', SYSTEM_PROMPT_AGENT_PLANNER
            )
            self.data.initial_message = self.data.initial_message or next(
                (msg['content'] for msg in chat_history if msg.get('role') == 'user'), ''
            )

        else:
            self.data = AgentDataModel(name="AgentNotInteractive")
            self.data.kwargs.update(kwargs)
            self.data.session_id = self.data.session_id or f"planner-{self.data.kwargs.get('session_id', None)}"
            self.data.is_interactive = self.data.is_interactive or self.data.kwargs.get('is_interactive', False)
            self.data.user_id = self.data.user_id or self.data.kwargs.get('user_id', None)
            self.data.chat_history = chat_history
            self.data.start_system_prompt = self.data.start_system_prompt or self.data.kwargs.get(
                'start_system_prompt', SYSTEM_PROMPT_AGENT_PLANNER
            )
            self.data.initial_message = self.data.initial_message or next(
                (msg['content'] for msg in chat_history if msg.get('role') == 'user'), ''
            )
      
        memory_handler = MemoryLogHandler(self.data.memory_logs)
        memory_handler.setLevel(logging.INFO)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        memory_handler.setFormatter(formatter)

        if not any(isinstance(h, MemoryLogHandler) for h in self.logger.handlers):
            self.logger.addHandler(memory_handler)


    def reset_to_init_data_model(self):
        """
        Resets the AgentDataModel to its initial state.
        This method clears or resets fields that should return to their default values.
        """
        self.logger.info('🔄 --------------------- Resetting AgentDataModel to initial state')

        reset_fields = {
            'initial_message': '',
            'json_chain': None,
            'state': 'idle',
            'agent_chain_step': 0,
            'sequential_agent_step': 0,
            'memory_logs': [],
            'thought_history': []
        }

        for field, value in reset_fields.items():
            setattr(self.data, field, value)

        self.logger.info('✅ --------------------- AgentDataModel has been reset to initial state')


    def sanitize_gpt_response(self, response_str: str) -> str:
        response_str = re.sub(r'^```json\s*', '', response_str, flags=re.MULTILINE)
        response_str = re.sub(r'```$', '', response_str, flags=re.MULTILINE)
        return response_str.strip() 

    def gen_prompt_for_dipendent_agents(self, agent_nickname: str, connected_agents: List[Dict], agent_llm_prompt: str) -> str:
        json_chain_copy = self.data.json_chain.copy()

        if agent_nickname != 'Aggregator':
            connected_agents_str = ""
            connected_agent_nicknames = []
            if connected_agents:
                connected_agent_nicknames = [agent['agent_nickname'] for agent in connected_agents]
                connected_agents_str = f"These are the agents nicknames from which your input comes: {', '.join(connected_agent_nicknames)}\n"

            for agent in json_chain_copy['agents']:
                if agent['agent_nickname'] not in connected_agent_nicknames:
                    agent.pop('observation', None)
            self.data.json_chain = json_chain_copy

            GENERATED_PROMPT = DIPENDENT_AGENT_PROMPT.format(
                agent_nickname=agent_nickname,
                agent_llm_prompt=agent_llm_prompt,
                connected_agents_str=connected_agents_str,
                json_chain_without_useless_info=json_chain_copy,
                initial_message=self.data.initial_message,
                user_questions=self.data.json_chain['agents'][self.data.agent_chain_step].get('user_questions', []),
                user_answers=self.data.json_chain['agents'][self.data.agent_chain_step].get('user_answers', [])
            )

            self.logger.info('\n\n\n🟣 --------------------- Generated prompt for agent %s:\n%s', agent_nickname, GENERATED_PROMPT)
            return GENERATED_PROMPT

        else:
            GENERATED_AGGREGATOR_PROMPT = AGGREGATOR_PROMPT.format(
                agent_nickname=agent_nickname,
                agent_llm_prompt=agent_llm_prompt,
                json_chain_without_useless_info=json_chain_copy,
                initial_message=self.data.initial_message
            )
            self.logger.info('\n\n\n🟣 --------------------- Generated prompt for final agent Aggregator\n: %s', GENERATED_AGGREGATOR_PROMPT)
            return GENERATED_AGGREGATOR_PROMPT

    def manage_user_questions(self, step: int) -> str:
        user_questions = self.data.json_chain['agents'][step].get('user_questions', [])
        user_answers = self.data.json_chain['agents'][step].get('user_answers', [])
        self.logger.info('⚪ --------------------- Updated User questions for agent %s: %s', self.data.json_chain['agents'][step]['agent_nickname'], user_questions)
        self.logger.info('⚪ --------------------- Updated User answers for agent %s: %s', self.data.json_chain['agents'][step]['agent_nickname'], user_answers)
        if len(user_questions) > len(user_answers):
            return user_questions[len(user_answers)]
        else:
            return None
               

    def run_parallel_agents(self, agents: List[Dict]):
        def process_agent(agent: Dict):
            try:
                self.data.agent_chain_step = next(
                    index for index, a in enumerate(self.data.json_chain['agents']) 
                    if a['agent_nickname'] == agent['agent_nickname']
                )
                
                connected_agents = [
                    a for a in self.data.json_chain['agents']
                    if 'observation' in a and a['agent_nickname'] in agent.get('input_from_agents', [])
                ]
                
                agent_prompt = self.gen_prompt_for_dipendent_agents(
                    agent['agent_nickname'], connected_agents, agent['agent_llm_prompt']
                )
                
                agent_output = call_openai_model(
                    prompt=agent_prompt,
                    model="o1-mini"
                )
                
                for ag in self.data.json_chain['agents']:
                    if ag['agent_nickname'] == agent['agent_nickname']:
                        ag['observation'] = agent_output
                        break
                
                self.logger.info(
                    '\n\n🟡 ---------------------Step nr %s, Generated observation for parallel agent %s\n: %s',
                    self.data.agent_chain_step, agent['agent_nickname'], agent_output
                )
            
            except Exception as e:
                self.logger.error(f"Error processing agent {agent['agent_nickname']}: {e}")


        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:

            futures = [executor.submit(process_agent, agent) for agent in agents]
            for future in concurrent.futures.as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    self.logger.error(f"Agent processing generated an exception: {e}")
                    

    def run_sequential_agents(self, agents: List[Dict]):

        start_step = self.data.sequential_agent_step

        for agent in agents[start_step:]:
            self.data.agent_chain_step = next(
                index for index, a in enumerate(self.data.json_chain['agents']) 
                if a['agent_nickname'] == agent['agent_nickname']
            )

            self.logger.info(f"agent_chain_step ------------------------------------------------: {self.data.agent_chain_step}")
            if self.data.state == 'waiting_for_user_answer':
                if 'user_answers' not in self.data.json_chain['agents'][self.data.agent_chain_step]:
                    self.data.json_chain['agents'][self.data.agent_chain_step]['user_answers'] = [self.data.chat_history[-1]['content']]
                else:
                    self.data.json_chain['agents'][self.data.agent_chain_step]['user_answers'].append(self.data.chat_history[-1]['content'])

            if self.data.is_interactive:
                new_user_question = self.manage_user_questions(self.data.agent_chain_step)
                if new_user_question:
                    self.data.final_answer = new_user_question
                    self.data.state = 'waiting_for_user_answer'
                    return True #temporary stop the script and give api response
                else:
                    self.data.state = 'running_chain'
            
            connected_agents = [
                a for a in self.data.json_chain['agents']
                if 'observation' in a and a['agent_nickname'] in agent.get('input_from_agents', [])
            ]
            agent_prompt = self.gen_prompt_for_dipendent_agents(
                agent['agent_nickname'], connected_agents, agent['agent_llm_prompt']
            )
            agent_output = call_openai_model(
                prompt=agent_prompt,
                model="o1-mini"
            )
            for ag in self.data.json_chain['agents']:
                if ag['agent_nickname'] == agent['agent_nickname']:
                    ag['observation'] = agent_output
                    break
            self.logger.info(
                '\n\n🟡 ---------------------Step nr %s, Generated observation for sequential agent %s\n: %s',
                self.data.agent_chain_step, agent['agent_nickname'], agent_output
            )
            self.data.sequential_agent_step += 1
        return False

    
    def run_single_agent(self, agent: Dict):
        self.data.agent_chain_step = next(
            index for index, a in enumerate(self.data.json_chain['agents']) 
            if a['agent_nickname'] == agent['agent_nickname']
        )
        agent_prompt = self.gen_prompt_for_dipendent_agents(
            agent['agent_nickname'], [], agent['agent_llm_prompt']
        )
        agent_output = call_openai_model(
            prompt=agent_prompt,
            model="o1-mini"
        )
        self.logger.info(
            '\n\n🟡 ---------------------Step nr %s, Generated observation for sequential agent %s\n: %s',
            self.data.agent_chain_step, agent['agent_nickname'], agent_output
        )
        return agent_output
    

    def elab_chain(self):
        agents = self.data.json_chain['agents']
        
        subtask_agents = agents[0:-1]

        # run subtask_agents with no user questions and no input from other agents
        agents_noquest_nodep = [
            agent for agent in subtask_agents
            if not agent.get('user_questions') and not agent.get('input_from_agents')
        ]
        if self.data.state != 'waiting_for_user_answer':
            self.run_parallel_agents(agents_noquest_nodep)

        agents_noquest_depwithobs = [
            agent for agent in subtask_agents
            if agent not in agents_noquest_nodep and not agent.get('user_questions') and all(
                any(a['agent_nickname'] == input_nickname and 'observation' in a and a['observation']
                    for a in self.data.json_chain['agents'])
                for input_nickname in agent.get('input_from_agents', [])
            )
        ]
        if self.data.state != 'waiting_for_user_answer':
            self.run_parallel_agents(agents_noquest_depwithobs)

        sequential_agents = [
            agent for agent in subtask_agents
            if agent not in agents_noquest_nodep and agent not in agents_noquest_depwithobs
        ]
        self.logger.info('🟣 --------------------- Running sequential agents: %s', sequential_agents)
        must_block = self.run_sequential_agents(sequential_agents)
        if must_block:
            return #temporary stop the script and give api response

        # run aggregator agent
        if self.data.sequential_agent_step == len(sequential_agents):
            aggregator_agent = agents[-1]
            aggregator_agent_output = self.run_single_agent(aggregator_agent)

            self.data.final_answer = aggregator_agent_output
            self.data.state = 'completed'
            self.reset_to_init_data_model()


    def run_planner(self):
        if self.data.state != 'waiting_for_user_answer':
            self.logger.info('\n\n🟢 --------------------- Starting planner')
            self.data.state = 'running_chain'
            response = call_openai_model(
                prompt=self.data.start_system_prompt.format(
                    initial_message=self.data.initial_message, 
                    json_chain_example=JSON_CHAIN_EXAMPLE
                ),
                model="o1-mini"
            )
            
            self.data.json_chain = json.loads(self.sanitize_gpt_response(response))
            if not self.data.is_interactive:
                self.logger.info('🟣 --------------------- Removing user questions from json chain for not interactive mode')
                for agent in self.data.json_chain.get('agents', []):
                    agent['user_questions'] = []
            self.logger.info(
                '\n\n\n🔵 --------------------- Generated initial json chain:\n%s', 
                json.dumps(self.data.json_chain, indent=4)
            )
            self.elab_chain()
        else:
            self.logger.info('\n\n🟢 --------------------- Received user answer, running chain')
            self.elab_chain()
        if self.data.is_interactive:
            self.session_manager.save_session(self.data)
            


