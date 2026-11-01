```
./.github/
├── ISSUE_TEMPLATE					# issue提交模板
│   └── comp_publish.yml			# 发布组件的Issue模板 
├── scripts							# 跑ci用的脚本
│   └── check_publish.py			# ai写的检查脚本，往gh发就是掉几个API，问ai就行了
└── workflows						# ci流程
    └── comp_publish_check.yml	# ci运行流程
```

除了`check_publish.py`之外我都写得有注释，或者这个地方不需要写注释，你照着看就行了。
`check_publish.py`我相信你的`Python`水平