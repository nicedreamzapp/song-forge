on launch_forge()
	set forgePath to (POSIX path of (path to home folder)) & "Desktop/PROJECTS/Song Forge"
	set supervisor to quoted form of (forgePath & "/forge_supervisor.sh")

	-- Boot all 3 servers (idempotent). Detached so the applet can exit.
	do shell script "/usr/bin/nohup /bin/bash " & supervisor & " </dev/null >/tmp/song_forge.log 2>&1 &"

	-- Wait for the hub to respond.
	do shell script "for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do /usr/bin/curl -s -o /dev/null http://localhost:8767/ && exit 0; /bin/sleep 0.5; done; exit 0"

	-- Reuse an existing Brave window pointing at the forge if there is one,
	-- otherwise open a fresh app window.
	set foundTab to false
	tell application "System Events"
		set braveRunning to (exists (processes whose name is "Brave Browser"))
	end tell
	if braveRunning then
		try
			tell application "Brave Browser"
				repeat with w in windows
					set tabIdx to 0
					repeat with t in tabs of w
						set tabIdx to tabIdx + 1
						if (URL of t) starts with "http://localhost:8767" then
							tell t to reload
							set active tab index of w to tabIdx
							set index of w to 1
							set foundTab to true
							exit repeat
						end if
					end repeat
					if foundTab then exit repeat
				end repeat
				if foundTab then activate
			end tell
		end try
	end if
	if not foundTab then
		do shell script "/usr/bin/open -na 'Brave Browser' --args --app=http://localhost:8767/ --user-data-dir=/tmp/song-forge --window-size=1480,900 --window-position=120,40 >/dev/null 2>&1"
	end if
end launch_forge

on run
	my launch_forge()
end run

on reopen
	my launch_forge()
end reopen
