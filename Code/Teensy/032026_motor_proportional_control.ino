//DM332T Switch Settings (SW1:SW2:SW3:SW4:SW5:SW6) -> 0 = off, 1 = on
//v1 -> 101111; v2 -> 101011

// ---------------------------------------------------------------------------
// Serial protocol (driven by the Python supervisor on the Raspberry Pi):
//   "<float>\n"   -> measured force value. Runs proportional control toward
//                    the current targetForce. Sets manualMode = false.
//   "T<float>\n"  -> set targetForce. Does not change mode.
//   'u'           -> manual mode, continuous upward steps until next command.
//   'd'           -> manual mode, continuous downward steps until next command.
//   's'           -> manual mode, stop.
// Safety: in automatic mode, motion auto-halts within 100 ms if Python stops
// streaming force values (lastForceTime watchdog). In manual mode, time-limiting
// of u/d is enforced on the Python side (jog auto-stop) since there is no
// homing or limit switch on the lead-screw ram.
// ---------------------------------------------------------------------------

String incoming = "";
float forceValue = 0.0;
float targetForce = 0.0;
float currentForce = 0.0;
float forceError = 0.0;
float deadband = 0.1;

const int DIR_Pin = 36;
const int STEP_Pin = 34;
unsigned long lastForceTime = 0;
unsigned long lastStepTime = 0;

char manualCommand = 's';
bool manualMode = true;


//Proportional Control Stuff:

float Kp_step = 20.0; //larger = more agressive response to error, v1 = 8.0
unsigned long minStepInterval = 8; //fastest, most agressive correction
unsigned long maxStepInterval = 75; //slowest correction near target, v1 = 50
unsigned long stepInterval = 50; //hold the computed step interval here (milliseconds)


void setup() {
  Serial.begin(115200);
  targetForce = 0.0;  // Python must explicitly set target via "T<value>\n" before any auto motion

  pinMode(DIR_Pin, OUTPUT);
  pinMode(STEP_Pin, OUTPUT);
}

void loop() {
  if (Serial.available() > 0){
    char c = Serial.read();

    //handle the next char fed in over serial
    
    if(c == 'u' || c == 'd' || c == 's'){
      manualMode = true;
      manualCommand = c;
      return;
    }
    if (c == '\n'){
      if (incoming.length() > 0) {  // guard: stray '\n' with empty buffer used to silently re-enter auto mode at 0.0 N
        if (incoming.charAt(0) == 'T') {
          // "T<float>\n" -> set target only, do not touch mode or watchdog
          targetForce = incoming.substring(1).toFloat();
        } else {
          forceValue = incoming.toFloat();
          currentForce = forceValue;
          forceError = targetForce - currentForce;
          stepInterval = constrain((unsigned long)(Kp_step / (abs(forceError) + 0.01)), minStepInterval, maxStepInterval);
          lastForceTime = millis();
          manualMode = false;
        }
      }
      incoming = "";
      }
    else{
      incoming += c;
      }
    }

  //do something with the input you collected
      
  if (manualMode){
    if(manualCommand == 'u'){
      digitalWrite(DIR_Pin, HIGH);
      digitalWrite(STEP_Pin, HIGH);
      delayMicroseconds(5);
      digitalWrite(STEP_Pin, LOW);
      delayMicroseconds(800);
    }
    if(manualCommand == 'd'){
      digitalWrite(DIR_Pin, LOW);
      digitalWrite(STEP_Pin, HIGH);
      delayMicroseconds(5);
      digitalWrite(STEP_Pin, LOW);
      delayMicroseconds(800);
      }
    if(manualCommand == 's'){
      //do nothing
      }
  }
  else if ((millis() - lastForceTime < 100) && (abs(forceError) > deadband) && (millis() - lastStepTime > stepInterval)){
    digitalWrite(DIR_Pin, forceError < 0); //forceError < 0
    digitalWrite(STEP_Pin, HIGH);
    delayMicroseconds(5);
    digitalWrite(STEP_Pin, LOW);
    lastStepTime = millis();
    }
  else{
    //error is within tolerance, do nothing
    }
}
