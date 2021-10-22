import os
import sys
sys.path.append(".")

import argparse

from openprompt.trainer import ClassificationRunner, GenerationRunner
from openprompt.lm_bff_trainer import LMBFFClassificationRunner
from re import template
from openprompt.pipeline_base import PromptForClassification, PromptForGeneration
from openprompt.utils.reproduciblity import set_seed
from openprompt import PromptDataLoader
from openprompt.prompts import load_template, load_verbalizer, load_template_generator, load_verbalizer_generator
from openprompt.data_utils import FewShotSampler
from openprompt.utils.logging import config_experiment_dir, init_logger, logger
from openprompt.config import get_yaml_config
from openprompt.plms import load_plm
from openprompt.data_utils import load_dataset
from openprompt.utils.utils import check_config_conflicts
from openprompt.utils.cuda import model_to_device


def get_config():
    parser = argparse.ArgumentParser("classification config")
    parser.add_argument("--config_yaml", type=str, help='the configuration file for this experiment.')
    parser.add_argument("--resume", type=str, help='a specified logging path to resume training.\
           It will fall back to run from initialization if no lastest checkpoint are found.')
    parser.add_argument("--test", type=str, help='a specified logging path to test')
    args = parser.parse_args()
    config = get_yaml_config(args.config_yaml)
    check_config_conflicts(config)
    logger.info("CONFIGS:\n{}\n{}\n".format(config, "="*40))
    return config, args


def build_dataloader(dataset, template, tokenizer, config, split):
    dataloader = PromptDataLoader(
        dataset = dataset, 
        template = template, 
        tokenizer = tokenizer, 
        batch_size = config[split].batch_size,
        shuffle = config[split].shuffle_data,
        teacher_forcing = config[split].teacher_forcing if hasattr(config[split],'teacher_forcing') else None,
        predict_eos_token = True if config.task == "generation" else False,
        **config.dataloader
    )
    return dataloader


def save_config_to_yaml(config):
    from contextlib import redirect_stdout
    saved_yaml_path = os.path.join(config.logging.path, "config.yaml")
    with open(saved_yaml_path, 'w') as f:
        with redirect_stdout(f): print(config.dump())
    logger.info("Config saved as {}".format(saved_yaml_path))


def main():
    config, args = get_config()
    # init logger, create log dir and set log level, etc.
    if args.resume and args.test:
        raise Exception("cannot use flag --resume and --test together")
    if args.resume or args.test:
        config.logging.path = EXP_PATH = args.resume or args.test
    else:
        EXP_PATH = config_experiment_dir(config)
        init_logger(os.path.join(EXP_PATH, "log.txt"), config.logging.file_level, config.logging.console_level)
        # save config to the logger directory
        save_config_to_yaml(config)

    # load dataset. The valid_dataset can be None
    train_dataset, valid_dataset, test_dataset, Processor = load_dataset(config, test = args.test is not None or config.learning_setting == 'zero_shot')

    # main
    if config.learning_setting == 'full':
        res = trainer(
            EXP_PATH,
            config,
            Processor,
            resume = args.resume,
            test = args.test,
            train_dataset = train_dataset,
            valid_dataset = valid_dataset,
            test_dataset = test_dataset,
        )
    elif config.learning_setting == 'few_shot':
        if config.few_shot.few_shot_sampling is None:
            raise ValueError("use few_shot setting but config.few_shot.few_shot_sampling is not specified")
        seeds = config.sampling_from_train.seed
        res = 0
        for seed in seeds:
            if not args.test:
                sampler = FewShotSampler(
                    num_examples_per_label = config.sampling_from_train.num_examples_per_label,
                    also_sample_dev = config.sampling_from_train.also_sample_dev,
                    num_examples_per_label_dev = config.sampling_from_train.num_examples_per_label_dev
                )
                train_sampled_dataset, valid_sampled_dataset = sampler(
                    train_dataset = train_dataset,
                    valid_dataset = valid_dataset,
                    seed = seed
                )
                result = trainer(
                    os.path.join(EXP_PATH, f"seed-{seed}"),
                    config,
                    Processor,
                    resume = args.resume,
                    test = args.test,
                    train_dataset = train_sampled_dataset,
                    valid_dataset = valid_sampled_dataset,
                    test_dataset = test_dataset,
                )
            else:
                result = trainer(
                    os.path.join(EXP_PATH, f"seed-{seed}"),
                    config,
                    Processor,
                    test = args.test,
                    test_dataset = test_dataset,
                )
            res += result
        res /= len(seeds)
    elif config.learning_setting == 'zero_shot':
        res = trainer(
            EXP_PATH,
            config,
            Processor,
            zero = True,
            train_dataset = train_dataset,
            valid_dataset = valid_dataset,
            test_dataset = test_dataset,
        )
    print("metric:", res)

def trainer(EXP_PATH, config, Processor, train_dataset = None, valid_dataset = None, test_dataset = None, resume = None, test = None, zero = False):
    # set seed
    set_seed(config.reproduce.seed)

    # load the pretrained models, its model, tokenizer, and config.
    plm_model, plm_tokenizer, plm_config = load_plm(config)

    # define template and verbalizer
    if config.task == "classification":
        verbalizer = load_verbalizer(config=config, model=plm_model, tokenizer=plm_tokenizer, plm_config=plm_config, classes=Processor.labels)
        template_generate_model, template_generate_tokenizer = None, None
        if config.classification.auto_t:
            template_generate_model, template_generate_tokenizer, template_generate_config = load_plm(config.template_generator)
            template = load_template(config=config, model=template_generate_model, tokenizer=template_generate_tokenizer, plm_config=template_generate_config, verbalizer=verbalizer)

        else:
            # define prompt
            template = load_template(config=config, model=plm_model, tokenizer=plm_tokenizer, plm_config=plm_config)
            
            # load prompt’s pipeline model
        prompt_model = PromptForClassification(plm_model, template, verbalizer, freeze_plm = config.plm.optimize.freeze_para)
            
    elif config.task == "generation":
        template = load_template(config=config, model=plm_model, tokenizer=plm_tokenizer, plm_config=plm_config)
        prompt_model = PromptForGeneration(plm_model, template, freeze_plm = config.plm.optimize.freeze_para, gen_config = config.generation)
    else:
        raise NotImplementedError(f"config.task {config.task} is not implemented yet. Only classification and generation are supported.")

    # process data and get data_loader
    if train_dataset:
        train_dataloader = build_dataloader(train_dataset, template, plm_tokenizer, config, "train")
    if valid_dataset:
        valid_dataloader = build_dataloader(valid_dataset, template, plm_tokenizer, config, "dev")
    if test_dataset:
        test_dataloader = build_dataloader(test_dataset, template, plm_tokenizer, config, "test")

    if config.task == "classification":
        if config.classification.auto_t or config.classification.auto_v:
            runner = LMBFFClassificationRunner(train_dataset = train_dataset, 
                                        valid_dataset = valid_dataset, 
                                        test_dataset = test_dataset, 
                                        model= plm_model, 
                                        tokenizer = plm_tokenizer, 
                                        template_generate_tokenizer = template_generate_tokenizer,
                                        template_generate_model = template_generate_model,
                                        initial_template = template,
                                        initial_verbalizer = verbalizer,
                                        config = config)
        else:
            runner = ClassificationRunner(
                model = prompt_model,
                train_dataloader = train_dataloader,
                valid_dataloader = valid_dataloader,
                test_dataloader = test_dataloader,
                config = config
            )
    elif config.task == "generation":
        runner = GenerationRunner(
            model = prompt_model,
            train_dataloader = train_dataloader,
            valid_dataloader = valid_dataloader,
            test_dataloader = test_dataloader,
            config = config
        )
        
    if zero:
        res = runner.test()
    elif test:
        res = runner.test(ckpt = 'best')
    elif resume:
        res = runner.run(ckpt = 'last')
    else:
        res = runner.run()
    return res


if __name__ == "__main__":
    main()
    
