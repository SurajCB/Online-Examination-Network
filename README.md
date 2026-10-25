# Online-Examination-Network
Multi-user online exam over raw TCP sockets on an SDN network (Mininet + Ryu/OpenFlow 1.3). The network learns about the exam from the app: it blocks intruders and brute-force logins, prioritises exam traffic with QoS queues and monitors it live.


Online Examination Network is a multi-user exam system where students take a timed MCQ exam over TCP sockets, inside a Mininet network controlled by a custom Ryu SDN controller. The exam server sends events to the controller: exam start, failed logins, submissions. The controller reacts by letting only registered seats through, blocking intruders and password guessers, putting exam traffic in a guaranteed-bandwidth queue and measuring traffic. Students join from their phones through a web portal, and examiners watch everything live on a password-protected Server Console. In our Mininet run it passed 8 of 8 automated checks, with an average response time of 7.94 ms and a Jain fairness index of 0.99.
