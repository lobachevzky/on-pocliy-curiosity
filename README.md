```
nice metacontroller                  --cuda-deterministic --agent-load-path="checkpoints/teacher.pt" --batch-size="2" --entropy-coef="0" --env="4x4InstructionsGridWorld-v0" --hidden-size="512" --interactions "visit" "pick-up" "transform" --learning-rate="0.1" --log-dir=/home/ethanbro/ppo/.runs/logdir/4x4Instructions/flat-control-flow/fix/42 --log-interval="10" --max-episode-steps="31" --max-task-count="1" --min-objects="0" --n-instructions="2" --num-layers="2" --num-processes="10" --num-steps="32" --object-types "sheep" "cat" "pig" "greenbot" --ppo-epoch="2" --run-id=4x4Instructions/flat-control-flow/fix/42 --save-interval="300" --seed="0" --metacontroller-entropy-coef="0.02" --metacontroller-hidden-size="512" --success-reward="-3" 
```