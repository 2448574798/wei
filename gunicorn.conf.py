import multiprocessing

bind = "127.0.0.1:8000"
workers = 2
timeout = 120
graceful_timeout = 30
keepalive = 5
accesslog = "/var/log/wei_agent_access.log"
errorlog = "/var/log/wei_agent_error.log"
loglevel = "info"
proc_name = "wei_agent"
