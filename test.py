import pprint, os, json, sqlite3, re, textwrap
import autogen
from autogen.agentchat import ConversableAgent, UserProxyAgent, Agent, AssistantAgent, GroupChat, GroupChatManager
from autogen.oai import config_list_from_json, filter_config
from autogen import runtime_logging
import numpy as np
import pandas as pd
from database import CervicalCancerDB
from database_Lung import LungCancerDB
from rag import RAGLung
from typing import Any, Callable, Dict, List, Literal, Optional, Tuple, Type, TypeVar, Union
from utils import generate_default_humanSupervisor_reply, generate_oai_reply_with_process_dosimetrist, generate_TPS_reply, generate_oai_reply_with_process_physicist
from radplan_qwen.before.prompts import get_iniTaskMsg_portpy_0806, get_prompt
from use_portpy import IMRT, JSON2Markdown 
import sys
import matplotlib.pyplot as plt

"""
import os
os.environ.pop("HTTP_PROXY", None)
os.environ.pop("HTTPS_PROXY", None)
os.environ.pop("http_proxy", None)
os.environ.pop("https_proxy", None)
os.environ.pop("ALL_PROXY", None)
os.environ.pop("all_proxy", None)

if False:
    os.environ["http_proxy"] = "http://127.0.0.1:8019"
"""

# os.environ["NO_PROXY"] = "deepseek.com,localhost,127.0.0.1"

# config_list = config_list_from_json(env_or_file="llm_config.json")

from llm_config import config_list
class AgentTeam:
    def __init__(self, db: LungCancerDB, rag: RAGLung, traj_folder, traj_file=None, optim_time_limit=1200, iter_sleep=300,
                  traj_llm_tag='gm-pro', sug_llm_tag='llama405B', dos_llm_tag='local-gpt-4o', phy_llm_tag='local-gpt-4o',
                  criteria_json_fn='./portpy_config_files/clinical_criteria/Default/Lung_2Gy_30Fx_wqx.json',
                  mode='random'):
        self.db = db
        self.rag = rag
        self.pid = None
        self.traj_folder = traj_folder
        self.traj_file = traj_file
        self.optim_time_limit = optim_time_limit
        self.criteria_json_fn = criteria_json_fn
        self.iter_sleep = iter_sleep
        self.traj_llm_tag = traj_llm_tag
        self.sug_llm_tag = sug_llm_tag
        self.dos_llm_tag = dos_llm_tag
        self.phy_llm_tag = phy_llm_tag
        self.mode = mode.lower()
    
    def get_initmsg(self, patient_name):
        '''patient_name: patient who needs treatment plan.  '''
        self.pid = patient_name
        # init_msg = get_iniTaskMsg_portpy_0727(patient_name)
        query_anatomy_str = self.rag.get_patient_anatomy_str(patient_name)
        
        # Only random and similarity use RAG
        if self.mode in ['random', 'similarity']:
            ref_plans_str = self.rag.get_ref_plans(patient_name)
        else:
            ref_plans_str = 'No reference plans provided in none-RAG mode.'
            
        init_msg = get_iniTaskMsg_portpy_0806(patient_name, query_anatomy_str, ref_plans_str, None)
        return init_msg

    def set_initializer(self):
        # initializer agent
        return UserProxyAgent(
            name="task_initializer",
            llm_config=False,
            code_execution_config=False,
            description="Initiate the planning process by providing patient details, prescribed dose, dose objectives, constraints, and any relevant reference plans or optimization parameters to guide the team."
        )

    def set_tps(self):
        # tps agent
        agent = ConversableAgent(
            name="TPS_proxy",
            llm_config=False,
            code_execution_config=False,
            human_input_mode="NEVER",
            description= "A TPS proxy simulates and optimizes the plan based on the set of optimization parameters."
        )
        agent.register_reply([Agent, None], generate_TPS_reply)  # set to 2 for huamn input first

        # additional attributes for optimization
        agent.imrt = IMRT(pid=self.pid, time_limit=self.optim_time_limit, criteria_json_fn=self.criteria_json_fn)
        agent.j2m = JSON2Markdown() 

        return agent

    def set_dosimetrist(self):
        # dosimetrist agent
        # use none_mode prompt only when mode is none
        prompt_name = 'sysmsg_dosimetris_portpy_none_mode_260330' if self.mode == 'none' else 'sysmsg_dosimetris_portpy_rag_0807_260330'
        agent = AssistantAgent(
            name="dosimetrist",
            llm_config=filter_config(config_list, {"tags":[self.dos_llm_tag]})[0],
            system_message=get_prompt(prompt_name),
            human_input_mode="NEVER",
            description="A senior dosimetrist proposes OptPara for TPS.",
        )
        agent.register_reply([Agent, None], generate_oai_reply_with_process_dosimetrist, position=4)  # set to 4 for human input first

        agent.agent_compareDosimetrist = ConversableAgent(
            name="Comparing Dosimetrist",
            system_message=get_prompt('sysmsg_CompareDosimetrist_portpy_0717'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_OptParaCompleteChecker = ConversableAgent(
            name="OptParaCompleteChecker",
            system_message=get_prompt('sysmsg_OptParaCompleteChecker_gemini_0721'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_ctriticalDosimetrist = ConversableAgent(
            name="Critical Dosimetrist",
            system_message=get_prompt('sysmsg_criticalDosimetrist_gemini_portpy_0710'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_SuggestDosimetrist = ConversableAgent(
            name="Suggest Dosimetrist",
            system_message=get_prompt('sysmsg_SuggestDosimetrist_gpt4_portpy_0719_260330'),
            llm_config=filter_config(config_list, {"tags":[self.sug_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_SuggestDosimetristCheck = ConversableAgent(
            name="SuggestDosimetristCheck",
            system_message=get_prompt('sysmsg_SuggestDosimetristCheck_portpy_0719'),
            llm_config=filter_config(config_list, {"tags":["gm-pro"]})[0],
            human_input_mode="NEVER"
        )

        return agent

    def set_physicist(self):
        # physicist agent
        agent = AssistantAgent(
            name="physicist",
            system_message=get_prompt('sysmsg_physicist_portpy_0715'),
            llm_config=filter_config(config_list, {"tags":[self.phy_llm_tag]})[0],
            description="A senior medical physicist evaluates the plan from a technical perspective",
        )
        agent.register_reply([Agent, None], generate_oai_reply_with_process_physicist)

        agent.agent_comparePhysicist = ConversableAgent(
            name="Comparing Physicist",
            system_message=get_prompt('sysmsg_ComparePhysicist_portpy_0712'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_trajReviewer = ConversableAgent(
            name="Trajectory Dosimetrist",
            system_message=get_prompt('sysmsg_Trajectory_gemini_portpy_0731'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        agent.agent_TrajVef = ConversableAgent(
            name="trajectoryVefDosimetrist",
            system_message=get_prompt('sysmsg_TrajVefDosimetrist_portpy_0719'),
            llm_config=filter_config(config_list, {"tags":[self.traj_llm_tag]})[0],
            human_input_mode="NEVER"
        )

        return agent

    def set_humanSupervisor(self):
        # human supervisor agent
        agent = UserProxyAgent(
            name="human_supervisor",
            llm_config=False,
            code_execution_config=False,
            human_input_mode="ALWAYS",
            description="A human supervisor provides guidance and feedback to the team members during the planning process."
        )
        agent.register_reply([Agent, None], generate_default_humanSupervisor_reply, position=2)  # set to 2 for check huamn input first
        # print(agent._reply_func_list)
        return agent

    def set_oncologist(self):
        agent = AssistantAgent(
            name="oncologist",
            system_message=get_prompt('sysmsg_oncologist_0526'),
            llm_config=filter_config(config_list, {"tags":["local-gpt-4o"]})[0],
            description="A senior radiation oncologist reviews the plan from a clinical perspective.",
        )
        agent.register_reply([Agent, None], generate_oai_reply_with_process_dosimetrist)
        return agent

    def set_team(self):
        # set all team members 
        initializer = self.set_initializer()
        dosimetrist = self.set_dosimetrist()
        tps_proxy = self.set_tps()
        physicist = self.set_physicist()
        human_supervisor = self.set_humanSupervisor() # 保留原有代码，方便以后恢复

        agents = [initializer, dosimetrist, tps_proxy, physicist] 

        allowed_transitions = {
            initializer: [dosimetrist],
            dosimetrist: [tps_proxy],
            tps_proxy: [physicist],
            physicist: [dosimetrist], # 改回直接给 dosimetrist
            # physicist: [human_supervisor],
            # human_supervisor: [dosimetrist],
        }

        groupchat = GroupChat(agents=agents,
                                    speaker_selection_method="round_robin",
                                    allowed_or_disallowed_speaker_transitions=allowed_transitions,
                                    speaker_transitions_type="allowed",
                                    messages=[], max_round=95, send_introductions=True)
        
        groupchat.DEFAULT_INTRO_MSG = get_prompt('groupchat_intromsg_0710')

        def _is_termination_msg(message: Dict[str, Any]) -> bool:
            content = message.get("content")
            if isinstance(content, list):
                # Best-effort: join text fragments if present.
                parts: List[str] = []
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(str(item.get("text", "")))
                    else:
                        parts.append(str(item))
                content_text = " ".join(parts)
            else:
                content_text = "" if content is None else str(content)
            return content_text.strip().upper().endswith("TERMINATE")

        manager = GroupChatManager(
            groupchat=groupchat,
            llm_config=False,
            system_message="You are the group chat manager...",
            is_termination_msg=_is_termination_msg,
        )
        
        # set manager attributes to share with agents
        manager.pid = self.pid
        manager.iter_count = 1
        manager.traj = ''
        manager.traj_full = []
        manager.traj_everyN = []
        manager.everyN = 5 
        manager.traj_folder = self.traj_folder
        manager.traj_file = self.traj_file
        manager.optim_time_limit = self.optim_time_limit
        manager.iter_sleep = self.iter_sleep
        manager.is_skip_physicist = False  # if OptPara and Dose outcomes can be found in file, skip physicist
        manager.reward_history = []  # Initialize reward history

        return initializer, manager        

    def run_auto_planning(self, init_msg):
        initializer, manager = self.set_team()

        logging_session_id = runtime_logging.start(config={"dbname": ".logs_lung_new.db"})

        initializer.initiate_chat(manager, message=init_msg)

        runtime_logging.stop()

        # Save reward history to CSV and PNG
        if hasattr(manager, 'reward_history') and manager.reward_history:
            history_folder = f"{manager.traj_folder}/{manager.pid}"
            if not os.path.exists(history_folder):
                os.makedirs(history_folder)
            
            # Export CSV
            df_reward = pd.DataFrame({
                'Iteration': range(1, len(manager.reward_history) + 1), 
                'Reward': manager.reward_history
            })
            csv_path = f"{history_folder}/reward_history.csv"
            df_reward.to_csv(csv_path, index=False)
            
            # Generate PNG Plot
            plt.figure(figsize=(10, 6))
            plt.plot(df_reward['Iteration'], df_reward['Reward'], marker='o', linestyle='-', color='#2A9D8F', linewidth=2)
            plt.fill_between(df_reward['Iteration'], df_reward['Reward'], alpha=0.1, color='#2A9D8F')
            plt.title(f'Reward Convergence Curve - {manager.pid}', fontsize=14, pad=15)
            plt.xlabel('Iteration Round', fontsize=12)
            plt.ylabel('RL Reward Score (utils.calculate_rl_reward)', fontsize=12)
            plt.grid(True, linestyle='--', alpha=0.7)
            plt.tight_layout()
            
            plot_path = f"{history_folder}/reward_curve.png"
            plt.savefig(plot_path, dpi=300)
            plt.close()
            print(f"\n[Summary] Reward history saved to: {csv_path}")
            print(f"[Summary] Reward curve plot saved to: {plot_path}\n")

    @staticmethod
    def unit_test(patient_name="Lung_Patient_23", rag_mode="random"): 
        # setup DB
        db = LungCancerDB()

        # setup RAG based on DB
        rag = RAGLung(db, mode=rag_mode)

        # setup autoplanning team
        traj_folder = f'./traj_{rag_mode}'
        team = AgentTeam(db, rag, traj_folder=traj_folder, mode=rag_mode)

        # setup planning task
        init_msg = team.get_initmsg(patient_name=patient_name) 

        # run auto planning
        team.run_auto_planning(init_msg) 

if __name__ == '__main__':
    # 接收命令行传入的参数
    patient = sys.argv[1] if len(sys.argv) > 1 else "Lung_Patient_23"
    rag_mode = (sys.argv[2] if len(sys.argv) > 2 else "random").strip().lower()
    valid_modes = {"none", "random", "similarity"}
    if rag_mode not in valid_modes:
        raise ValueError(f"Invalid rag_mode: {rag_mode}. Allowed modes: {sorted(valid_modes)}")

    AgentTeam.unit_test(patient, rag_mode)