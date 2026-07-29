#!/usr/bin/env python3
"""QLoRA SFT of a code model on bgcommit repair pairs. Fits a 24GB GPU (4-bit base).

    python train_qlora.py                       # deepseek, defaults
    python train_qlora.py --dry-run             # validate data/tokenizer/config, no GPU
    python train_qlora.py --model Qwen/Qwen2.5-Coder-7B-Instruct --epochs 2

Saves, under --output-dir (default finetune/output/<model>-bgcommit-lora):
  - adapter_model.safetensors + adapter_config.json   (the LoRA weights — reuse these)
  - the tokenizer                                      (so the dir is self-contained)
  - checkpoint-*/                                      (periodic, resumable)
  - training_metadata.json                             (base model, args, data counts)
Reuse: load base + PeftModel.from_pretrained(<dir>), or run merge_and_export.py to bake
the adapter into a standalone model vLLM can serve.
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")

# deepseek-coder / Qwen2.5-Coder / CodeLlama are all LLaMA-arch → same target modules
LORA_TARGETS = ["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"]


def build_dataset(tokenizer, path, max_len):
    """Tokenize chat examples; mask everything before the assistant turn (completion-only
    training — the model learns the fix, not to echo the prompt)."""
    import datasets
    rows = [json.loads(l) for l in open(path) if l.strip()]

    def enc(ex):
        msgs = ex["messages"]
        # template -> string -> tokenize (tokenize=True returns a non-serializable
        # Encoding in transformers 5.x; the template already carries special tokens).
        full_s = tokenizer.apply_chat_template(msgs, tokenize=False, add_generation_prompt=False)
        prompt_s = tokenizer.apply_chat_template(msgs[:-1], tokenize=False, add_generation_prompt=True)
        full = tokenizer(full_s, add_special_tokens=False)["input_ids"]
        prompt = tokenizer(prompt_s, add_special_tokens=False)["input_ids"]
        full, prompt = full[:max_len], prompt[:max_len]
        labels = [-100] * len(prompt) + full[len(prompt):]
        labels = labels[:len(full)] + [-100] * max(0, len(full) - len(labels))
        return {"input_ids": full, "attention_mask": [1] * len(full), "labels": labels}

    ds = datasets.Dataset.from_list(rows).map(enc, remove_columns=["messages", "meta"])
    ds = ds.filter(lambda r: any(t != -100 for t in r["labels"]))   # keep only supervised
    return ds


class Collator:
    def __init__(self, pad_id):
        self.pad = pad_id

    def __call__(self, batch):
        import torch
        m = max(len(b["input_ids"]) for b in batch)
        ids, att, lab = [], [], []
        for b in batch:
            p = m - len(b["input_ids"])
            ids.append(b["input_ids"] + [self.pad] * p)
            att.append(b["attention_mask"] + [0] * p)
            lab.append(b["labels"] + [-100] * p)
        return {"input_ids": torch.tensor(ids), "attention_mask": torch.tensor(att),
                "labels": torch.tensor(lab)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="deepseek-ai/deepseek-coder-6.7b-instruct")
    ap.add_argument("--output-dir", default=None)
    ap.add_argument("--epochs", type=float, default=2.0)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--grad-accum", type=int, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--max-len", type=int, default=1024)
    ap.add_argument("--lora-r", type=int, default=16)
    ap.add_argument("--lora-alpha", type=int, default=32)
    ap.add_argument("--dry-run", action="store_true",
                    help="validate data/tokenizer/config and exit — no model load, no GPU")
    args = ap.parse_args()

    out = args.output_dir or os.path.join(
        HERE, "output", os.path.basename(args.model) + "-bgcommit-lora")

    from transformers import AutoTokenizer
    tok = AutoTokenizer.from_pretrained(args.model)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token

    train_ds = build_dataset(tok, os.path.join(DATA, "train.jsonl"), args.max_len)
    val_ds = build_dataset(tok, os.path.join(DATA, "val.jsonl"), args.max_len)
    lens = [len(r["input_ids"]) for r in train_ds]
    sup = [sum(1 for t in r["labels"] if t != -100) for r in train_ds]
    print(f"train {len(train_ds)} / val {len(val_ds)}  | seq len: "
          f"median {sorted(lens)[len(lens)//2]}, max {max(lens)}  | supervised tokens/ex: "
          f"median {sorted(sup)[len(sup)//2]}")

    if args.dry_run:
        from peft import LoraConfig
        from transformers import TrainingArguments
        LoraConfig(r=args.lora_r, lora_alpha=args.lora_alpha, target_modules=LORA_TARGETS,
                   lora_dropout=0.05, bias="none", task_type="CAUSAL_LM")
        TrainingArguments(output_dir=out, num_train_epochs=args.epochs,
                          per_device_train_batch_size=args.batch,
                          gradient_accumulation_steps=args.grad_accum, learning_rate=args.lr)
        print("dry-run OK: data tokenizes, completion masking works, configs build.")
        print(f"would save weights to: {out}")
        return

    import torch
    from transformers import (AutoModelForCausalLM, BitsAndBytesConfig,
                              TrainingArguments, Trainer)
    from peft import LoraConfig, get_peft_model, prepare_model_for_kbit_training

    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                             bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(
        args.model, quantization_config=bnb, device_map="auto", dtype=torch.bfloat16)
    model = prepare_model_for_kbit_training(model, use_gradient_checkpointing=True)
    model = get_peft_model(model, LoraConfig(
        r=args.lora_r, lora_alpha=args.lora_alpha, target_modules=LORA_TARGETS,
        lora_dropout=0.05, bias="none", task_type="CAUSAL_LM"))
    model.print_trainable_parameters()
    model.config.use_cache = False

    targs = TrainingArguments(
        output_dir=out, num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch, gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr, lr_scheduler_type="cosine", warmup_ratio=0.03,
        bf16=True, gradient_checkpointing=True, gradient_checkpointing_kwargs={"use_reentrant": False},
        optim="paged_adamw_8bit", logging_steps=20,
        save_strategy="epoch", save_total_limit=2, eval_strategy="epoch",
        report_to="none")
    trainer = Trainer(model=model, args=targs, train_dataset=train_ds, eval_dataset=val_ds,
                      data_collator=Collator(tok.pad_token_id), processing_class=tok)
    trainer.train()

    # ── save weights for reuse ────────────────────────────────────────────────
    model.save_pretrained(out)          # adapter_model.safetensors + adapter_config.json
    tok.save_pretrained(out)
    json.dump({"base_model": args.model, "epochs": args.epochs, "lr": args.lr,
               "lora_r": args.lora_r, "lora_alpha": args.lora_alpha, "max_len": args.max_len,
               "n_train": len(train_ds), "n_val": len(val_ds)},
              open(os.path.join(out, "training_metadata.json"), "w"), indent=2)
    print(f"\n✓ saved LoRA weights + tokenizer -> {out}")
    print("  reuse: PeftModel.from_pretrained(base, '%s')  or  python merge_and_export.py" % out)


if __name__ == "__main__":
    main()
