import torch as T
from torch.cuda.amp import GradScaler
import numpy as np
from tqdm import tqdm
import gc
import sys
from tokenizers import Tokenizer
from tokenizers.models import WordPiece
from torch.utils.data import Dataset, DataLoader
from handler import DataHandler 
from model import *



MICRO_BATCH_SIZE = 32

def train(
        n_epochs=1,
        lr=1e-4,
        batch_size=1024,
        dtype=T.float16,
        model_file='../trained_models/CrammingModel.pt',
        num_workers=4,
        pin_memory=True,
        input_dims=128,
        embed_dims=768, 
        num_heads=12, 
        has_bias=False,
        dropout_rate=0.0,
        mlp_hidden_dims=None,
        n_encoder_blocks=12,
        mlp_expansion_factor=4,
        output_dims=None,
        use_gpu=True
        ) -> None:
    T.cuda.empty_cache()

    loss_fn = T.nn.CrossEntropyLoss()
    device  = T.device('cuda:0' if use_gpu else 'cpu')

    handler = DataHandler(
            dataset_name='bookcorpus', 
            max_length=input_dims,
            dtype=dtype,
            device=device
            )

    ## Set `batch_size` to MICRO_BATCH_SIZE for memory reasons. Will make backward pass only 
    ## when actual `batch_size` inputs are processed.
    dataloader = handler.get_dataloader(batch_size=MICRO_BATCH_SIZE)

    cramming_model = CrammingTransformer(
            vocab_size=handler.tokenizer.vocab_size,
            input_dims=input_dims,
            embed_dims=embed_dims,
            num_heads=num_heads, 
            has_bias=has_bias,
            dropout_rate=dropout_rate,
            mlp_hidden_dims=mlp_hidden_dims,
            n_encoder_blocks=n_encoder_blocks,
            mlp_expansion_factor=mlp_expansion_factor,
            lr=lr,
            device=device
            )

    grad_scaler = GradScaler()

    progress_bar = tqdm(total=len(dataloader) * n_epochs)

    back_losses = []
    for idx, (X, attention_mask, y, mask_mask) in enumerate(dataloader):
        X              = X.to(cramming_model.device)
        attention_mask = attention_mask.to(cramming_model.device)
        y              = y.to(cramming_model.device)
        mask_mask      = mask_mask.to(cramming_model.device)

        with T.autocast(device_type='cuda', dtype=T.float16):
            out = cramming_model.forward(X, attention_mask)

            y        = (mask_mask * y).flatten()
            out      = (mask_mask.unsqueeze(dim=-1) * out).flatten(start_dim=0, end_dim=1)

            loss_idxs = T.argwhere(y != 0)
            out       = out[loss_idxs, :].squeeze()
            _y        = T.zeros_like(out)
            _y[:, y[loss_idxs]] = 1
            
            loss = loss_fn(out, _y)

        T.nn.utils.clip_grad_norm_(cramming_model.parameters(), max_norm=2.0)
        grad_scaler.scale(loss).backward()

        if idx % (batch_size // MICRO_BATCH_SIZE) == 0:
            grad_scaler.step(cramming_model.optimizer)
            grad_scaler.update()
            cramming_model.scheduler.step()

            ## Zeroing grad like this is faster. (https://h-huang.github.io/tutorials/recipes/recipes/tuning_guide.html)
            cramming_model.optimizer.zero_grad()
            for param in cramming_model.parameters():
                param.grad = None
            
        back_losses.append(loss.item())

        if idx % 1000 == 0:
            cramming_model.save_model(model_file=model_file)
            print(f'Preds: {out}\n\n', f'Largest pred_prob: {T.max(out, dim=1)}\n\n\n\n')
            print(
                    f'Original text: {handler.tokenizer.decode(T.argmax(_y, dim=1))}\n\n', 
                    f'Original text: {handler.tokenizer.decode(X[0])}\n\n', 
                    f'Predicted Text: {handler.tokenizer.decode(T.argmax(out, dim=1))}\n\n'
                    )
            sys.exit()

        progress_bar.update(1)
        progress_bar.set_description(f'Running Loss: {np.mean(back_losses[-100:])}')



if __name__ == '__main__':
    train()
