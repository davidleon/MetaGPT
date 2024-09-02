from expo.utils import DATA_CONFIG
import os
import pandas as pd
from expo.evaluation.evaluation import evaluate_score
import datetime
import json
from expo.MCTS import create_initial_state
from expo.research_assistant import ResearchAssistant


class Experimenter:
    result_path : str = "results/base"
    data_config = DATA_CONFIG
    

    def __init__(self, args, **kwargs):
        self.args = args
        self.start_time = datetime.datetime.now().strftime("%Y%m%d%H%M")

    async def run_experiment(self):
        state = create_initial_state(self.args.task, start_task_id=1, data_config=self.data_config, low_is_better=self.args.low_is_better, name="")
        user_requirement = state["requirement"]
        di = ResearchAssistant(node_id="0", use_reflection=self.args.reflection)
        await di.run(user_requirement)
    
        score_dict = await di.get_score()
        score_dict = self.evaluate(score_dict, state)
        results = {
            "score_dict": score_dict,
            "user_requirement": user_requirement,
            "args": vars(self.args)
        }
        self.save_result(results)

    def evaluate_prediction(self, split, state):
        pred_path = os.path.join(state["work_dir"], state["task"], f"{split}_predictions.csv")
        os.makedirs(state["node_dir"], exist_ok=True)
        pred_node_path = os.path.join(state["node_dir"], f"{self.start_time}-{split}_predictions.csv")
        gt_path = os.path.join(state["datasets_dir"][f"{split}_target"])
        preds = pd.read_csv(pred_path)["target"]
        preds.to_csv(pred_node_path, index=False)
        gt = pd.read_csv(gt_path)["target"]
        metric = state["dataset_config"]["metric"]
        return evaluate_score(preds, gt, metric)
    
    def evaluate(self, score_dict, state):
        scores = {
            "dev_score": self.evaluate_prediction("dev", state),
            "test_score": self.evaluate_prediction("test", state),
        }
        score_dict.update(scores)
        return score_dict


    def save_result(self, result):
        os.makedirs(self.result_path, exist_ok=True)
        with open(f"{self.result_path}/{self.args.exp_mode}-{self.args.task}_{self.start_time}.json", "w") as f:
            json.dump(result, f, indent=4)
