
# basic calling and defining funcions ############################
def greeting(): # greet is the function name 
    print("hello world")

def meet():
    print("nice to meet you")

#greeting()
#meet()

# passing argument and using peramiters #############################
    # an argument is a value sent to a function when called 
    # peramiter are variable inside a function thet rteceve those values 

def greet(fill_in_whatever):
    
    print("hello " + fill_in_whatever)

#greet("aiz")

# default peramiters ###########################################################
def show_tax(price, tax_rate = .07):
    print(price * tax_rate)
#show_tax(100) 
# return statments ###################################################
def square(x): 
    return x * x
#result = square(4) # point to the square() function fillingn in the variabele w/ 4 and sets it to the variable = result 

#print(result)
#######################################################################

#greeting()
#meet()
#greet("aiz") # arg is a string
#show_tax(100) #arg is a int
#print(square(4)) # arg is an int

def main():
    greeting()

main()
