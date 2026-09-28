#!/usr/bin/bash

# This script is used to run the application and check if it's down, then run it automatically

# Check if the application is running

if pgrep -f "~/gpscj.net_bot/telegram_bot.py" > /dev/null;
then
    echo "Application is already running."
    echo $(pgrep -f "~/gpscj.net_bot/telegram_bot.py" at $(date "+%Y-%m-%d %H:%M:%S")) >> ~/gpscj.net_bot/mon_journal.log
else
    echo "Application is not running. Starting the application..."
    # Start the application
    #.venv/bin/python3 ~/gpscj.net_bot/telegram_bot.py > ~/gpscj.net_bot/mon_journal.log 2>&1 &
    # Run the application in the background
    nohup .venv/bin/python3 ~/gpscj.net_bot/telegram_bot.py > ~/gpscj.net_bot/mon_journal.log 2>&1 &
    if pgrep -f "/home/xq/gps/gps/telegram_bot.py" > /dev/null;
    then
        echo "Application started successfully. --> Process ID: $(pgrep -f "~/gpscj.net_bot/telegram_bot.py") --> Log file: ~/gpscj.net_bot/mon_journal.log at $(date "+%Y-%m-%d %H:%M:%S")" >  ~/gpscj.net_bot/mon_journal.log
    else
        echo "Failed to start the application. at $(date "+%Y-%m-%d %H:%M:%S")" WARNING >> ~/gpscj.net_bot/mon_journal.log
    fi 
fi
#kill -9 $(pgrep -f "~/gpscj.net_bot/telegram_bot.py")
# crontab
## * * * * * bash ~//run.sh
