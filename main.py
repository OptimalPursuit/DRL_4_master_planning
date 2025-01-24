## Imports
import os
import copy

# Datatypes
import yaml
from dotmap import DotMap
from typing import Optional
from tensordict.nn import TensorDictModule, TensorDictSequential

# Machine learning
import torch
import wandb

# TorchRL
from torchrl.envs.utils import check_env_specs
from torchrl.modules import TruncatedNormal, ValueOperator

# Custom:
# Training
from rl_algorithms.utils import make_env, adapt_env_kwargs
from rl_algorithms.train import run_training
# Models
from models.embeddings import MPPInitEmbedding, MPPContextEmbedding, MPPDynamicEmbedding, MPPObservationEmbedding
from models.autoencoder import Autoencoder
from models.encoder import MLPEncoder, AttentionEncoder
from models.decoder import AttentionDecoderWithCache, MLPDecoderWithCache
from models.critic import CriticNetwork
from rl_algorithms.projection import ProjectionFactory
from rl_algorithms.projection_prob_actor import ProjectionProbabilisticActor
from rl_algorithms.test import evaluate_model

# Functions
def load_config(config_path: str) -> DotMap:
    with open(config_path, 'r') as file:
        config = yaml.safe_load(file)
        config = DotMap(config)
        config = adapt_env_kwargs(config)
    return config


def initialize_encoder(encoder_type, encoder_args, device):
    """Initialize the encoder based on the type."""
    if encoder_type == "attention":
        return AttentionEncoder(**encoder_args).to(device)
    elif encoder_type == "mlp":
        return MLPEncoder(**encoder_args).to(device)
    else:
        raise ValueError(f"Unsupported encoder type: {encoder_type}")

def initialize_decoder(decoder_type, decoder_args, device):
    """Initialize the decoder based on the type."""
    if decoder_type == "attention":
        return AttentionDecoderWithCache(**decoder_args).to(device)
    elif decoder_type == "mlp":
        return MLPDecoderWithCache(**decoder_args).to(device)
    else:
        raise ValueError(f"Unsupported decoder type: {decoder_type}")

def initialize_critic(algorithm_type, encoder, critic_args, device):
    """Initialize the critic based on the algorithm type."""
    if algorithm_type == "sac":
        return TensorDictModule(
            CriticNetwork(encoder, customized=True, use_q_value=True, **critic_args).to(device),
            in_keys=["observation", "action"],
            out_keys=["state_action_value"])
    else:
        # Standard critic
        return TensorDictModule(
            CriticNetwork(encoder, customized=True, **critic_args).to(device),
            in_keys=["observation"],
            out_keys=["state_value"]
        )

def initialize_projection_layer(projection_type, projection_kwargs, action_dim, n_constraints):
    """Initialize the projection layer based on the projection type."""
    projection_type = (projection_type or "").lower()  # Normalize to lowercase and handle None
    projection_kwargs["n_action"] = action_dim
    projection_kwargs["n_constraints"] = n_constraints
    return ProjectionFactory.create_class(projection_type, projection_kwargs)

def initialize_policy_and_critic(config, env, device):
    """
    Initializes the policy and critic models based on the given configuration and environment.

    Args:
        config: Configuration object containing model, training, and algorithm settings.
        env: Environment object containing action specifications and other parameters.
        device: The device (CPU/GPU) to initialize the models on.

    Returns:
        policy: The initialized policy model.
        critic: The initialized critic model.
    """
    # Validate input
    assert hasattr(config, 'model'), "Config object must have a 'model' attribute."
    assert hasattr(env, 'action_spec'), "Environment must have an 'action_spec' attribute."

    # Embedding dimensions
    embed_dim = config.model.embed_dim
    action_dim = env.action_spec.shape[0]
    sequence_dim = env.K * env.T if env.action_spec.shape[0] == env.B * env.D else env.P - 1

    # Embedding initialization
    init_embed = MPPInitEmbedding(action_dim, embed_dim, sequence_dim, env)
    context_embed = MPPContextEmbedding(action_dim, embed_dim, sequence_dim, env, config.model.demand_aggregation)
    dynamic_embed = MPPDynamicEmbedding(embed_dim, sequence_dim, env)
    obs_embed = MPPObservationEmbedding(action_dim, embed_dim, sequence_dim, env, config.model.demand_aggregation)

    # Model arguments
    decoder_args = {
        "embed_dim": embed_dim,
        "action_dim": action_dim,
        "seq_dim": sequence_dim,
        "init_embedding": init_embed,
        "context_embedding": context_embed,
        "dynamic_embedding": dynamic_embed,
        "obs_embedding": obs_embed,
        **config.model,
    }
    encoder_args = {
        "embed_dim": embed_dim,
        "init_embedding": init_embed,
        "env_name": env.name,
        **config.model,
    }
    critic_args = {
        "embed_dim": embed_dim,
        "action_dim": action_dim,
        "obs_embedding": obs_embed,
        **config.model,
    }

    # Init models: encoder, decoder, and critic
    encoder = initialize_encoder(config.model.encoder_type, encoder_args, device)
    decoder = initialize_decoder(config.model.decoder_type, decoder_args, device)
    critic = initialize_critic(config.algorithm.type, encoder, critic_args, device)

    # Init projection layer
    projection_layer = initialize_projection_layer(
        config.training.projection_type,
        config.training.projection_kwargs,
        action_dim,
        env.n_constraints
    )

    # Init actor (policy)
    actor = TensorDictModule(
        Autoencoder(encoder, decoder, env).to(device),
        in_keys=["observation"],  # Input tensor key in TensorDict
        out_keys=["loc", "scale"]  # Output tensor key in TensorDict
    )
    policy = ProjectionProbabilisticActor(
        module=actor,
        in_keys=["loc", "scale"],
        distribution_class=TruncatedNormal,
        distribution_kwargs={"low": env.action_spec.low[0], "high": env.action_spec.high[0]},
        return_log_prob=True,
        projection_layer=projection_layer,
        projection_type=config.training.projection_type,
        spec=env.action_spec
    )

    return policy, critic


# Main function
def main(config: Optional[DotMap] = None):
    ## Torch and cuda initialization
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    torch.cuda.empty_cache()
    torch._dynamo.config.cache_size_limit = 64  # or some higher value
    torch.set_num_threads(1)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

    ## Environment initialization
    # todo: add parallel environment runs; # env = ParallelEnv(4, make_env)
    env = make_env(config.env)
    env.set_seed(config.env.seed)
    check_env_specs(env)

    ## Main loop
    # Train the model
    if config.model.phase == "train":
        # Initialize models and run training
        policy, critic = initialize_policy_and_critic(config, env, device)
        run_training(policy, critic, **config)
    # Test the model
    elif config.model.phase == "test":
        # Get path to the trained model
        timestamp = config.testing.timestamp
        algorithm = config.algorithm.type
        projection = config.training.projection_type
        path = f"saved_models/{algorithm}/{projection}/{timestamp}"

        # Extract trained hyperparameters
        config_load_path = f"{path}/config.yaml"
        loaded_config = load_config(config_load_path)

        # Initialize models
        policy, critic = initialize_policy_and_critic(loaded_config, env, device)

        # Reload policy model
        policy_load_path = f"{path}/policy.pth"
        policy.load_state_dict(torch.load(policy_load_path, map_location=device))
        check_nans_model(policy)

        # Evaluate the model
        metrics, summary_stats = evaluate_model(policy, loaded_config, device=device, **config.testing)
        print(summary_stats) # todo: add visualization of the metrics/summary_stats

def check_nans_model(model):
    for name, param in model.named_parameters():
        if torch.isnan(param).any():
            print(f"NaN detected in {name}")

if __name__ == "__main__":
    # Load static configuration from the YAML file
    file_path = os.getcwd()
    config = load_config(f'{file_path}/config.yaml')

    # Call your main() function
    try:
        wandb.init(config=config,)
        model = main(config)
    except Exception as e:
        # Log the error to WandB
        wandb.log({"error": str(e)})

        # Optionally, use WandB alert for critical errors
        wandb.alert(
            title="Training Error",
            text=f"An error occurred during training: {e}",
            level="error"  # 'info' or 'warning' levels can be used as needed
        )

        # Print the error for local console logging as well
        print(f"An error occurred during training: {e}")
    finally:
        wandb.finish()